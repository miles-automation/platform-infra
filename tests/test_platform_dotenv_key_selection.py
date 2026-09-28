import importlib.util
import pytest
from argparse import Namespace
import json
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Mapping


def _platform_module() -> ModuleType:
    path = Path(__file__).parents[1] / "bin" / "platform"
    loader = SourceFileLoader("platform_cli_dotenv", str(path))
    spec = importlib.util.spec_from_loader("platform_cli_dotenv", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dotenv_parser_uses_last_value_like_docker_compose(tmp_path: Path) -> None:
    platform = _platform_module()
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SPARK_SWARM_API_KEY=stale-first\n"
        "IGNORED=value\n"
        "SPARK_SWARM_API_KEY='active-last'\n"
    )

    result = subprocess.run(
        [sys.executable, "-", str(dotenv)],
        input=platform._DOTENV_LAST_VALUE_PY,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == "active-last"


PEM = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49+/abc=\nxyz\n-----END PRIVATE KEY-----"


def _apply(tmp_path: Path, existing: str, secrets: Mapping[str, object], allowed: object = None) -> subprocess.CompletedProcess[str]:
    platform = _platform_module()
    env = tmp_path / ".env"
    env.write_text(existing)
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"project": "p", "environment": "production", "secrets": secrets}))
    return subprocess.run(
        [sys.executable, "-", str(env), str(export), json.dumps(allowed), "# BEGIN SPARKSWARM p production",
         "# END SPARKSWARM p production", str(tmp_path / "out.env")],
        input=platform._DOTENV_APPLY_PY, text=True, capture_output=True,
    )


def test_project_export_does_not_shadow_shared_credentials(tmp_path: Path) -> None:
    result = _apply(tmp_path, "", {"SPARK_SWARM_API_KEY": "global-fallback", "WEAVER_OWNER_TOKEN": "test$value",
                                   "CODE_LOOM_IMAGE_TAG": "sha-test"}, ["WEAVER_OWNER_TOKEN", "CODE_LOOM_IMAGE_TAG"])
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.env").read_text() == (
        '# BEGIN SPARKSWARM p production\nWEAVER_OWNER_TOKEN="test$$value"\nCODE_LOOM_IMAGE_TAG=sha-test\n'
        "# END SPARKSWARM p production\n"
    )


def test_unconfigured_exports_keep_every_secret(tmp_path: Path) -> None:
    result = _apply(tmp_path, "KEEP=me\n", {"KEY": "value", "OTHER": "also-kept"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.env").read_text() == (
        "KEEP=me\n# BEGIN SPARKSWARM p production\nKEY=value\nOTHER=also-kept\n# END SPARKSWARM p production\n"
    )


def test_multiline_and_special_values_stay_on_one_line(tmp_path: Path) -> None:
    result = _apply(tmp_path, "", {"PEM": PEM, "QUOTES": 'a "b" \'c\' #d', "BACKSLASH": "a\\nb", "EMPTY": ""})
    assert result.returncode == 0, result.stderr
    body = (tmp_path / "out.env").read_text().splitlines()
    assert body[1] == 'PEM="-----BEGIN PRIVATE KEY-----\\nMIGTAgEAMBMGByqGSM49+/abc=\\nxyz\\n-----END PRIVATE KEY-----"'
    assert body[2] == 'QUOTES="a \\"b\\" \'c\' #d"'
    assert body[3] == 'BACKSLASH="a\\\\nb"'
    assert body[4] == "EMPTY="
    assert len(body) == 6


def test_existing_definitions_are_replaced_not_duplicated(tmp_path: Path) -> None:
    existing = (
        "SPARK_SWARM_API_KEY=k\nPEM=old-single-line\nOTHER=kept\n"
        "# BEGIN SPARKSWARM p production\nPEM=-----BEGIN PRIVATE KEY-----\nbroken\n-----END PRIVATE KEY-----\n"
        "# END SPARKSWARM p production\n"
        'QUOTED="line one\nline two"\nTAIL=kept\n'
    )
    result = _apply(tmp_path, existing, {"PEM": PEM, "QUOTED": "new"})
    assert result.returncode == 0, result.stderr
    out = (tmp_path / "out.env").read_text()
    assert out.count("PEM=") == 1
    assert out.count("QUOTED=") == 1
    assert "broken" not in out and "line two" not in out
    assert out.startswith("SPARK_SWARM_API_KEY=k\nOTHER=kept\nTAIL=kept\n# BEGIN SPARKSWARM p production\n")


def test_invalid_secret_names_are_refused(tmp_path: Path) -> None:
    result = _apply(tmp_path, "", {"BAD NAME": "x"})
    assert result.returncode != 0
    assert "invalid dotenv name" in result.stderr
    assert not (tmp_path / "out.env").exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose not available")
def test_docker_compose_reads_back_the_exact_values(tmp_path: Path) -> None:
    secrets = {"PEM": PEM, "QUOTES": 'a "b" \'c\' #d', "DOLLAR": "p$ss${HOME}", "BACKSLASH": "a\\nb\\\\c",
               "SPACES": "  padded value  ", "PLAIN": "abc+/==",
               "ROOM": "!room:chat.example.com", "PUNCT": "a?b&c;d<e>(f){g}[h]|i^j~k*l!m"}
    result = _apply(tmp_path, "", secrets)
    assert result.returncode == 0, result.stderr
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  probe:\n    image: busybox\n    environment:\n"
        + "".join(f"      {name}: ${{{name}}}\n" for name in secrets)
    )
    config = subprocess.run(
        ["docker", "compose", "--env-file", str(tmp_path / "out.env"), "config", "--format", "json"],
        cwd=tmp_path, text=True, capture_output=True, check=True,
    )
    rendered = json.loads(config.stdout)["services"]["probe"]["environment"]
    assert {name: value.replace("$$", "$") for name, value in rendered.items()} == secrets


def test_apply_embeds_configured_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = _platform_module()
    commands = []
    monkeypatch.setattr(platform, "sh", lambda command, **kwargs: commands.append(command))
    args = Namespace(
        cfg={"prod": {"droplet_host": "example.invalid", "platform_infra_dir": "/test"},
             "secrets": {"api_base_url": "https://example.invalid"},
             "projects": {"weaver": {"secret_export_allowlist": ["WEAVER_OWNER_TOKEN"]}}},
        project="weaver", environment="production", yes=True, dry_run=True, quiet=True,
    )
    platform.cmd_prod_secrets_apply(args)
    assert len(commands) == 1
    assert "'[\"WEAVER_OWNER_TOKEN\"]'" in commands[0][2]
    assert "/secrets/export?project=weaver&environment=production" in commands[0][2]


def test_isolated_export_preserves_legacy_runtime_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    platform = _platform_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(platform, "sh", lambda command, **kwargs: commands.append(command))
    args = Namespace(
        cfg={"prod": {"droplet_host": "example.invalid", "platform_infra_dir": str(tmp_path)},
             "secrets": {"api_base_url": "https://example.invalid"},
             "projects": {"mail": {"secrets_project": "human-index", "secret_export_marker": "human-index-mail",
                                     "secret_export_allowlist": ["HI_MAIL_WORKER_KEY"]}}},
        project="mail", environment="production", yes=True, dry_run=True, quiet=True,
    )
    platform.cmd_prod_secrets_apply(args)
    legacy = "SPARK_SWARM_API_KEY=synthetic\n# BEGIN SPARKSWARM human-index production\nLEGACY_PASSWORD=preserve\n# END SPARKSWARM human-index production\n"
    (tmp_path / ".env").write_text(legacy)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text("#!/bin/sh\nprintf '%s\\n' '{\"secrets\": {\"HI_MAIL_WORKER_KEY\": \"synthetic-new\"}}'\n")
    curl.chmod(0o700)
    import os
    for _ in range(2):
        subprocess.run(["bash", "-c", commands[0][2]], check=True,
                       env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]})
    result = (tmp_path / ".env").read_text()
    assert result.startswith(legacy)
    assert result.count("# BEGIN SPARKSWARM human-index-mail production") == 1
    assert "project=human-index&environment=production" in commands[0][2]
    assert "HI_MAIL_WORKER_KEY=synthetic-new" in result


def test_multiline_value_with_trailing_comment_does_not_eat_following_lines(tmp_path: Path) -> None:
    existing = 'KEY="l1\nl2" # note\nKEEP1=a\nKEEP2=b\nLAST="z"\n'
    result = _apply(tmp_path, existing, {"KEY": "new"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.env").read_text().startswith('KEEP1=a\nKEEP2=b\nLAST="z"\n# BEGIN SPARKSWARM p production\n')


def test_unterminated_quote_is_refused_without_output(tmp_path: Path) -> None:
    result = _apply(tmp_path, 'KEY="never closed\nKEEP=a\n# BEGIN SPARKSWARM other production\nX=1\n', {"KEY": "new"})
    assert result.returncode != 0
    assert "unterminated quoted value" in result.stderr
    assert not (tmp_path / "out.env").exists()


def test_continuation_line_that_looks_like_a_definition_is_kept(tmp_path: Path) -> None:
    existing = 'OTHER="line1\nKEY=not-a-definition\nend"\nKEY=old\n'
    result = _apply(tmp_path, existing, {"KEY": "new"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.env").read_text() == (
        'OTHER="line1\nKEY=not-a-definition\nend"\n# BEGIN SPARKSWARM p production\nKEY=new\n'
        "# END SPARKSWARM p production\n"
    )


def test_unicode_line_separators_in_other_values_survive_reapply(tmp_path: Path) -> None:
    existing = "ODD=a b\x85c\x0bd\r\nKEY=old\n"
    result = _apply(tmp_path, existing, {"KEY": "new"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out.env").read_bytes().decode().startswith("ODD=a b\x85c\x0bd\r\n# BEGIN")


def test_systemd_readable_values_stay_unquoted(tmp_path: Path) -> None:
    result = _apply(tmp_path, "", {"MATRIX_ROOM_ID": "!room:chat.example.com", "URL": "https://x.example/a?b=c&d=e"})
    assert result.returncode == 0, result.stderr
    body = (tmp_path / "out.env").read_text()
    assert "MATRIX_ROOM_ID=!room:chat.example.com\n" in body
    assert "URL=https://x.example/a?b=c&d=e\n" in body


def _apply_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    platform = _platform_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(platform, "sh", lambda command, **kwargs: commands.append(command))
    args = Namespace(
        cfg={"prod": {"droplet_host": "example.invalid", "platform_infra_dir": str(tmp_path)},
             "secrets": {"api_base_url": "https://example.invalid"},
             "projects": {"p": {}}},
        project="p", environment="production", yes=True, dry_run=True, quiet=True,
    )
    platform.cmd_prod_secrets_apply(args)
    return commands[0][2]


def _fake_bin(tmp_path: Path, docker_exit: int) -> dict[str, str]:
    import os
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "curl").write_text("#!/bin/sh\nprintf '%s\\n' '{\"secrets\": {\"PEM\": \"MIGsecret+/=\\\\nline\"}}'\n")
    (fake_bin / "docker").write_text(
        f"#!/bin/sh\necho 'unexpected character in variable name \"MIGsecret+/=\"' >&2\nexit {docker_exit}\n"
    )
    for tool in ("curl", "docker"):
        (fake_bin / tool).chmod(0o700)
    return {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}


def test_compose_rejection_leaves_env_untouched_and_prints_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _apply_script(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("SPARK_SWARM_API_KEY=k\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    result = subprocess.run(["bash", "-c", script], env=_fake_bin(tmp_path, 1), text=True, capture_output=True)
    assert result.returncode != 0
    assert "MIGsecret" not in result.stdout + result.stderr
    assert (tmp_path / ".env").read_text() == "SPARK_SWARM_API_KEY=k\n"
    assert not list(tmp_path.glob(".env.apply.*"))


def test_backups_keep_the_five_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _apply_script(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("SPARK_SWARM_API_KEY=k\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z", "20200104T000000Z",
                  "20200105T000000Z", "20200106T000000Z"):
        (tmp_path / f".env.bak.secrets-{stamp}").write_text("old\n")
    result = subprocess.run(["bash", "-c", script], env=_fake_bin(tmp_path, 0), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    backups = sorted(path.name for path in tmp_path.glob(".env.bak.secrets-*"))
    assert len(backups) == 5
    assert ".env.bak.secrets-20200101T000000Z" not in backups
    assert "applied 'p/production' secrets" in result.stdout or "applied p/production secrets" in result.stdout


def test_crlf_block_markers_are_replaced_not_duplicated(tmp_path: Path) -> None:
    existing = "KEEP=a\r\n# BEGIN SPARKSWARM p production\r\nKEY=old\r\n# END SPARKSWARM p production\r\n"
    result = _apply(tmp_path, existing, {"KEY": "new"})
    assert result.returncode == 0, result.stderr
    out = (tmp_path / "out.env").read_bytes().decode()
    assert out.count("# BEGIN SPARKSWARM p production") == 1
    assert "KEY=old" not in out
