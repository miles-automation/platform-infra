from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "ci"))

import dispatcher as d  # noqa: E402
import runner  # noqa: E402
import worker  # noqa: E402

from test_ci_dispatcher import FakeClock, FakeCloud, FakeGitHub  # noqa: E402


@dataclass
class ScriptedClient:
    heartbeats: list[str] = field(default_factory=list)
    completions: list[tuple[int, str]] = field(default_factory=list)
    complete_failures: int = 0

    def heartbeat(self, job: dict[str, Any]) -> str:
        return self.heartbeats.pop(0) if self.heartbeats else "ok"

    def complete(self, job: dict[str, Any], outcome: str) -> bool:
        if self.complete_failures:
            self.complete_failures -= 1
            raise runner.DispatcherUnavailable("down")
        self.completions.append((int(job["id"]), outcome))
        return True


JOB = {"id": 1, "lease": "l", "action": "check", "repo": "o/r", "sha": "a" * 40, "project": "p"}


def test_lost_lease_kills_the_running_command_and_suppresses_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []
    monkeypatch.setattr(worker, "GITHUB_TOKEN", "t")
    def fake_api(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        posted.append(url)
        return 201, {}

    monkeypatch.setattr(worker, "_gh_api", fake_api)
    client = ScriptedClient(heartbeats=["lost"])
    log = str(tmp_path / "job.log")

    def slow(job: dict[str, Any]) -> str:
        rc = worker._run(["sleep", "30"], cwd=None, logfile=log)
        worker.set_status("o/r", "a" * 40, "failure", "should not post")
        return "failure" if rc else "success"

    started = time.monotonic()
    outcome = runner.run_job(client, JOB, 0.05, slow)  # type: ignore[arg-type]
    assert outcome == "cancelled"
    assert time.monotonic() - started < 15
    assert posted == []
    worker.begin_job()
    worker.set_status("o/r", "a" * 40, "success", "posts again")
    assert len(posted) == 1


def test_completion_report_retries_until_the_dispatcher_answers() -> None:
    client = ScriptedClient(complete_failures=2)
    sleeps: list[float] = []
    runner.report(client, JOB, "success", 1.0, sleeps.append)  # type: ignore[arg-type]
    assert client.completions == [(1, "success")]
    assert sleeps == [1.0, 1.0]


def test_check_status_names_the_configured_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "platform.toml").write_text('[projects.slopticus]\ncheck_target = "ci"\n[projects.plain]\n')
    monkeypatch.setattr(worker, "WORKSPACE", str(tmp_path))
    assert worker.check_target("slopticus") == "ci"
    assert worker.check_target("plain") == "check"
    assert worker.check_target("missing") == "check"


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[d.Dispatcher, str, FakeGitHub]]:
    cfg = d.Config(
        webhook_secret="s",
        runner_token="runner-token",
        admin_token="admin-token",
        repo_map={"o/r": "p"},
        db_path=str(tmp_path / "db.sqlite"),
    )
    gh = FakeGitHub()
    disp = d.Dispatcher(cfg=cfg, cloud=FakeCloud(), github=gh, clock=FakeClock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), d.make_handler(disp))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield disp, f"http://127.0.0.1:{server.server_address[1]}", gh
    server.shutdown()
    server.server_close()
    disp.close()


def test_runner_pulls_runs_and_completes_over_http(live: tuple[d.Dispatcher, str, FakeGitHub]) -> None:
    disp, url, _ = live
    disp.handle_webhook("push", "d1", {"ref": "refs/heads/main", "after": "b" * 40,
                                       "repository": {"full_name": "o/r", "default_branch": "main"}})
    disp.tick()
    box = disp.status()["box"]
    assert box is not None
    stranger = runner.DispatcherClient(runner.RunnerConfig(url, "runner-token", "424242"))
    with pytest.raises(runner.UnknownRunner):
        stranger.claim()
    cfg = runner.RunnerConfig(url, "runner-token", str(box["droplet_id"]), heartbeat_seconds=0.05)
    client = runner.DispatcherClient(cfg)
    ran: list[str] = []
    job = client.claim()
    assert job is not None and job["action"] == "build_deploy"

    def fake(j: dict[str, Any]) -> str:
        time.sleep(0.2)
        ran.append(str(j["sha"]))
        return "success"

    outcome = runner.run_job(client, job, cfg.heartbeat_seconds, fake)
    runner.report(client, job, outcome, 0.01)
    assert ran == ["b" * 40]
    assert disp.status()["recent"][0]["state"] == "done"
    assert client.claim() is None
    bad = runner.DispatcherClient(runner.RunnerConfig(url, "wrong", str(box["droplet_id"])))
    with pytest.raises(runner.DispatcherUnavailable):
        bad.claim()
