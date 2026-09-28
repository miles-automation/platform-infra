from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "ci"))

import dispatcher as d  # noqa: E402

from test_ci_dispatcher import FakeClock, FakeCloud, FakeGitHub  # noqa: E402


def platform_module() -> ModuleType:
    path = Path(__file__).parents[1] / "bin" / "platform"
    loader = SourceFileLoader("platform_ci_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDo:
    def __init__(self, tagged: list[int], snapshots: list[dict[str, Any]], final: str = "completed") -> None:
        self.tagged = tagged
        self.snapshots = snapshots
        self.final = final
        self.calls: list[tuple[str, str]] = []

    def __call__(self, token: str, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, path))
        if path.startswith("/droplets?tag_name="):
            return {"droplets": [{"id": i} for i in self.tagged]}
        if method == "POST" and path.endswith("/actions"):
            assert data is not None and data["type"] == "snapshot"
            self.pending = str(data["name"])
            return {"action": {"id": 9, "status": "in-progress"}}
        if "/actions/" in path:
            if self.final == "completed":
                self.snapshots.append({"id": 99, "name": self.pending, "created_at": "2026-09-28T00:00:00Z"})
            return {"action": {"id": 9, "status": self.final}}
        if path.startswith("/droplets/"):
            return {"droplet": {"name": "platform-ci-ondemand-x", "status": "active"}}
        if path.startswith("/snapshots?"):
            return {"snapshots": list(self.snapshots)}
        if method == "DELETE":
            return {}
        raise AssertionError(path)


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"cfg": {}, "droplet_id": None, "keep": 2, "timeout_minutes": 1.0, "yes": True, "dry_run": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _snap(i: int, day: int) -> dict[str, Any]:
    return {"id": i, "name": f"platform-ci-snap-2026092{day}", "created_at": f"2026-09-2{day}T00:00:00Z"}


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = platform_module()
    monkeypatch.setenv("PLATFORM_CI_SNAPSHOT_DO_TOKEN", "t")
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module


def test_snapshot_prunes_only_after_success_and_keeps_newest(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDo([42], [_snap(1, 1), _snap(2, 2), _snap(3, 3), {"id": 5, "name": "platform-pre-pg18", "created_at": "2026-09-01"}])
    monkeypatch.setattr(cli, "_do", fake)
    cli.cmd_ci_snapshot(_args())
    deletes = [p for (m, p) in fake.calls if m == "DELETE"]
    assert deletes == ["/snapshots/2", "/snapshots/1"]
    assert ("POST", "/droplets/42/actions") in fake.calls


def test_snapshot_failure_never_prunes(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDo([42], [_snap(1, 1), _snap(2, 2), _snap(3, 3)], final="errored")
    monkeypatch.setattr(cli, "_do", fake)
    with pytest.raises(SystemExit, match="errored"):
        cli.cmd_ci_snapshot(_args())
    assert not [p for (m, p) in fake.calls if m == "DELETE"]


def test_snapshot_refuses_ambiguous_box_and_dry_run_writes_nothing(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDo([], [])
    monkeypatch.setattr(cli, "_do", fake)
    with pytest.raises(SystemExit, match="exactly one"):
        cli.cmd_ci_snapshot(_args())
    fake = FakeDo([], [])
    monkeypatch.setattr(cli, "_do", fake)
    cli.cmd_ci_snapshot(_args(droplet_id=551995541, dry_run=True, yes=False))
    assert all(m == "GET" for (m, _) in fake.calls)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[d.Dispatcher, str]]:
    cfg = d.Config(webhook_secret="s", runner_token="r", admin_token="admin", repo_map={"o/r": "p"},
                   db_path=str(tmp_path / "db.sqlite"))
    disp = d.Dispatcher(cfg=cfg, cloud=FakeCloud(), github=FakeGitHub(), clock=FakeClock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), d.make_handler(disp))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield disp, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    disp.close()


def test_status_and_hold_talk_to_the_dispatcher(
    cli: ModuleType, served: tuple[d.Dispatcher, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    disp, url = served
    monkeypatch.setenv("PLATFORM_CI_ADMIN_TOKEN", "admin")
    disp.handle_webhook("push", "x", {"ref": "refs/heads/main", "after": "c" * 40,
                                      "repository": {"full_name": "o/r", "default_branch": "main"}})
    args = argparse.Namespace(cfg={"ci": {"dispatcher_url": url}}, json=False, limit=5, minutes=30.0)
    cli.cmd_ci_status(args)
    out = capsys.readouterr().out
    assert "box: none" in out and "queued: 1" in out and "o/r@ccccccc" in out
    cli.cmd_ci_hold(args)
    assert disp.status()["hold_until"] > disp.clock()
    monkeypatch.setenv("PLATFORM_CI_ADMIN_TOKEN", "wrong")
    with pytest.raises(SystemExit, match="HTTP 401"):
        cli.cmd_ci_status(args)
