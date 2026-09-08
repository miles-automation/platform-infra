import argparse
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest


def platform_module() -> ModuleType:
    path = Path(__file__).parents[1] / "bin" / "platform"
    loader = SourceFileLoader("platform_delivery_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_rollouts_exclude_competing_processes_and_release_after_failure(tmp_path: Path) -> None:
    module = platform_module()
    with pytest.raises(ValueError):
        with module.rollout_lock(tmp_path, "spark-swarm"):
            with pytest.raises(SystemExit, match="Another rollout owns"):
                with module.rollout_lock(tmp_path, "spark-swarm"):
                    raise AssertionError("Competing rollout acquired the lock")
            with module.rollout_lock(tmp_path, "weaver"):
                pass
            raise ValueError("Rollout failure")
    with module.rollout_lock(tmp_path, "spark-swarm"):
        pass


def test_release_status_only_emits_selected_nonsecret_fields(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    module = platform_module()
    cfg = {"projects": {"spark-swarm": {"infra_service": "spark-swarm"}}, "prod": {"droplet_host": "test-host", "platform_infra_dir": str(tmp_path), "platform_infra_compose_file": str(tmp_path / "docker-compose.yml")}}
    (tmp_path / ".env").write_text("SECRET_PASSWORD=never-output-this\nSPARK_SWARM_IMAGE_TAG=sha-abcdef0\n")
    container = {"Config": {"Image": "image:sha-abcdef0", "Env": ["SECRET_PASSWORD=never-output-this"], "Labels": {"org.opencontainers.image.revision": "a" * 40}}, "Image": "sha256:" + "a" * 64, "State": {"Running": True}}
    def remote(args: list[str], **kwargs: object) -> str:
        assert args[:2] == ["ssh", "test-host"]
        script = args[2].split("\n", 1)[1].rsplit("\n", 1)[0]
        import io
        from contextlib import redirect_stdout
        def output(command: list[str], **options: object) -> str:
            return json.dumps([container]) if command[1] == "inspect" else "container-id\n"
        with monkeypatch.context() as patch:
            patch.setattr(module.subprocess, "check_output", output)
            stream = io.StringIO()
            with redirect_stdout(stream):
                exec(compile(script, "release-status", "exec"), {})
            return stream.getvalue()
    monkeypatch.setattr(module, "sh_capture", remote)
    args = argparse.Namespace(cfg=cfg, target="spark-swarm", dry_run=False)
    module.cmd_prod_release_status(args)
    text = capsys.readouterr().out
    assert "never-output-this" not in text
    value = json.loads(text)
    assert value["desiredTag"] == "sha-abcdef0"
    assert value["imageId"] == container["Image"]
    assert value["revision"] == "a" * 40
    assert value["running"] is True


def test_release_status_rejects_unconfigured_target_before_remote_inspection() -> None:
    module = platform_module()
    with pytest.raises(SystemExit, match="configured project service"):
        module.cmd_prod_release_status(argparse.Namespace(cfg={"projects": {}}, target="raw-service"))
