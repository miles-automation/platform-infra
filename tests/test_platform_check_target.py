import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _platform_module() -> ModuleType:
    path = Path(__file__).parents[1] / "bin" / "platform"
    loader = SourceFileLoader("platform_cli_check_target", str(path))
    spec = importlib.util.spec_from_loader("platform_cli_check_target", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace(tmp_path: Path) -> dict[str, Any]:
    for name in ("plain", "full"):
        (tmp_path / "repos" / name).mkdir(parents=True)
    return {
        "projects": {
            "plain": {"repo_dir": "repos/plain", "worktree_prefix": "wt/plain-"},
            "full": {"repo_dir": "repos/full", "worktree_prefix": "wt/full-", "check_target": "ci"},
        }
    }


def _check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cfg: dict[str, Any],
    project: str | None,
    target: str | None,
) -> list[list[str]]:
    platform = _platform_module()
    calls: list[list[str]] = []

    def record(cmd: list[str], **_: object) -> int:
        calls.append(cmd)
        return 0

    monkeypatch.setattr(platform, "sh", record)
    platform.cmd_check(
        argparse.Namespace(
            workspace_root=tmp_path,
            cfg=cfg,
            project=project,
            target=target,
            wt=None,
            dry_run=False,
            quiet=True,
        )
    )
    return calls


def test_check_runs_each_projects_configured_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _check(monkeypatch, tmp_path, _workspace(tmp_path), None, None)
    assert calls == [
        ["make", "-C", str(tmp_path / "repos" / "full"), "ci"],
        ["make", "-C", str(tmp_path / "repos" / "plain"), "check"],
    ]


def test_explicit_target_overrides_the_configured_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _check(monkeypatch, tmp_path, _workspace(tmp_path), "full", "check")
    assert calls == [["make", "-C", str(tmp_path / "repos" / "full"), "check"]]


def test_invalid_check_target_fails_the_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _workspace(tmp_path)
    cfg["projects"]["full"]["check_target"] = ""
    with pytest.raises(SystemExit, match="Checks failed: full"):
        _check(monkeypatch, tmp_path, cfg, "full", None)
