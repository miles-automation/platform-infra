import importlib.util
from argparse import Namespace
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _platform_module():
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


def test_project_export_does_not_shadow_shared_credentials(tmp_path: Path) -> None:
    platform = _platform_module()
    dotenv = tmp_path / "export.env"
    dotenv.write_text("SPARK_SWARM_API_KEY=global-fallback\nWEAVER_OWNER_TOKEN=test$value\nCODE_LOOM_IMAGE_TAG=sha-test\n")
    subprocess.run(
        [sys.executable, "-", str(dotenv), '["WEAVER_OWNER_TOKEN", "CODE_LOOM_IMAGE_TAG"]'],
        input=platform._DOTENV_EXPORT_FILTER_PY,
        text=True,
        check=True,
    )
    assert dotenv.read_text() == "WEAVER_OWNER_TOKEN=test$$value\nCODE_LOOM_IMAGE_TAG=sha-test\n"


def test_unconfigured_exports_keep_existing_behavior(tmp_path: Path) -> None:
    platform = _platform_module()
    dotenv = tmp_path / "export.env"
    dotenv.write_text("KEY=value\nOTHER=also-kept\n")
    subprocess.run(
        [sys.executable, "-", str(dotenv), 'null'],
        input=platform._DOTENV_EXPORT_FILTER_PY,
        text=True,
        check=True,
    )
    assert dotenv.read_text() == "KEY=value\nOTHER=also-kept\n"


def test_apply_embeds_configured_allowlist(monkeypatch) -> None:
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
    assert "python3 - \"$tmp\" '[\"WEAVER_OWNER_TOKEN\"]'" in commands[0][2]
