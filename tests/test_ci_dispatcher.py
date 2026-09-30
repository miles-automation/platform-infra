from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "ci"))

import dispatcher as d  # noqa: E402

REPO = "miles-automation/slopticus"
OTHER = "miles-automation/human-index-v2"


@dataclass
class FakeClock:
    now: float = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeCloud:
    droplets: dict[int, d.Droplet] = field(default_factory=dict)
    snapshot: d.Snapshot | None = field(default_factory=lambda: d.Snapshot(7, "platform-ci-snap-1", "2026-09-27"))
    create_error: str | None = None
    list_error: str | None = None
    created: list[d.DropletSpec] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    next_id: int = 100

    def list_droplets(self, tag: str) -> list[d.Droplet]:
        if self.list_error:
            raise d.CloudError(self.list_error)
        return [x for x in self.droplets.values() if tag in x.tags]

    def latest_snapshot(self, prefix: str) -> d.Snapshot | None:
        return self.snapshot if self.snapshot and self.snapshot.name.startswith(prefix) else None

    def create_droplet(self, spec: d.DropletSpec) -> d.Droplet:
        if self.create_error:
            raise d.CloudError(self.create_error)
        self.next_id += 1
        droplet = d.Droplet(self.next_id, spec.name, "new", spec.tags)
        self.droplets[droplet.id] = droplet
        self.created.append(spec)
        return droplet

    def delete_droplet(self, droplet_id: int) -> None:
        self.deleted.append(droplet_id)
        self.droplets.pop(droplet_id, None)

    def add(self, droplet_id: int, name: str, tag: str = "platform-ci-ondemand") -> None:
        self.droplets[droplet_id] = d.Droplet(droplet_id, name, "active", (tag,))


@dataclass
class FakeGitHub:
    statuses: list[tuple[str, str, str, str]] = field(default_factory=list)
    heads: dict[tuple[str, int], d.PullHead] = field(default_factory=dict)
    fail: bool = False

    def set_status(self, repo: str, sha: str, state: str, description: str, context: str) -> bool:
        if self.fail:
            return False
        self.statuses.append((repo, sha, state, description))
        return True

    def pr_head(self, repo: str, number: int) -> d.PullHead | None:
        return self.heads.get((repo, number))

    def last(self, sha: str) -> tuple[str, str]:
        found = [(s, desc) for (_, x, s, desc) in self.statuses if x == sha]
        assert found, f"no status for {sha}"
        return found[-1]


@dataclass
class Rig:
    disp: d.Dispatcher
    cloud: FakeCloud
    gh: FakeGitHub
    clock: FakeClock
    cfg: d.Config

    def restart(self) -> Rig:
        self.disp.close()
        disp = d.Dispatcher(cfg=self.cfg, cloud=self.cloud, github=self.gh, clock=self.clock)
        disp.recover()
        return Rig(disp, self.cloud, self.gh, self.clock, self.cfg)

    def pr(self, sha: str, number: int = 1, action: str = "synchronize", repo: str = REPO,
           delivery: str | None = None) -> tuple[int, str]:
        payload = {"action": action, "number": number, "repository": {"full_name": repo},
                   "pull_request": {"number": number, "head": {"sha": sha}}}
        return self.disp.handle_webhook("pull_request", delivery or f"pr-{sha}-{action}", payload)

    def push(self, sha: str, repo: str = REPO, delivery: str | None = None) -> tuple[int, str]:
        payload = {"ref": "refs/heads/main", "after": sha,
                   "repository": {"full_name": repo, "default_branch": "main"}}
        return self.disp.handle_webhook("push", delivery or f"push-{sha}", payload)

    def tick(self) -> None:
        self.disp.tick()
        self.disp.drain_outbox()

    def boot(self) -> str:
        self.tick()
        assert self.disp.status()["box"] is not None
        return str(self.disp.status()["box"]["droplet_id"])

    def claim(self, runner: str) -> dict[str, Any] | None:
        job = self.disp.claim(runner)
        self.disp.drain_outbox()
        return job

    def states(self) -> dict[str, str]:
        return {j["sha"]: j["state"] for j in self.disp.status()["recent"]}


def make_rig(tmp_path: Path, check_only: frozenset[str] = frozenset()) -> Rig:
    cfg = d.Config(
        webhook_secret="hook-secret",
        runner_token="runner-token",
        admin_token="admin-token",
        repo_map={REPO: "slopticus", OTHER: "human-index-v2"},
        db_path=str(tmp_path / "dispatcher.sqlite"),
        check_only=check_only,
    )
    clock = FakeClock()
    cloud = FakeCloud()
    gh = FakeGitHub()
    return Rig(d.Dispatcher(cfg=cfg, cloud=cloud, github=gh, clock=clock), cloud, gh, clock, cfg)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = make_rig(tmp_path)
    yield r
    r.disp.close()


def test_no_work_means_no_box(rig: Rig) -> None:
    for _ in range(5):
        rig.clock.advance(60)
        rig.tick()
    assert rig.cloud.created == []
    assert rig.disp.status()["box"] is None


def test_pr_push_creates_one_box_from_latest_snapshot_and_runs_to_status(rig: Rig) -> None:
    assert rig.pr("a" * 40) == (202, "queued check")
    rig.disp.drain_outbox()
    assert rig.gh.last("a" * 40)[0] == "pending"
    runner = rig.boot()
    rig.clock.advance(120)
    rig.tick()
    assert len(rig.cloud.created) == 1
    spec = rig.cloud.created[0]
    assert (spec.image, spec.size, spec.region, spec.tags) == (7, "s-4vcpu-8gb", "nyc3", ("platform-ci-ondemand",))
    job = rig.claim(runner)
    assert job is not None and job["action"] == "check" and job["sha"] == "a" * 40
    assert rig.disp.heartbeat(runner, job["id"], job["lease"]) == "ok"
    assert rig.disp.complete(runner, job["id"], job["lease"], "success")
    assert rig.claim(runner) is None
    assert rig.states()["a" * 40] == "done"


def test_queue_survives_dispatcher_restart(rig: Rig) -> None:
    rig.pr("a" * 40)
    rig.push("b" * 40)
    rig = rig.restart()
    runner = rig.boot()
    first = rig.claim(runner)
    assert first is not None and first["sha"] == "a" * 40
    assert rig.disp.complete(runner, first["id"], first["lease"], "success")
    second = rig.claim(runner)
    assert second is not None and second["sha"] == "b" * 40


def test_restart_does_not_expire_running_leases(rig: Rig) -> None:
    rig.push("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    rig.clock.advance(rig.cfg.lease_timeout_seconds + 30)
    rig = rig.restart()
    rig.tick()
    assert rig.states()["a" * 40] == "leased"
    assert rig.disp.complete(runner, job["id"], job["lease"], "success")


def test_newer_pr_head_supersedes_queued_check_without_failing_it(rig: Rig) -> None:
    rig.pr("a" * 40, number=5)
    rig.pr("b" * 40, number=5)
    rig.pr("c" * 40, number=6)
    rig.disp.drain_outbox()
    state, desc = rig.gh.last("a" * 40)
    assert state == "success" and "superseded" in desc and "bbbbbbb" in desc
    assert rig.states() == {"a" * 40: "superseded", "b" * 40: "queued", "c" * 40: "queued"}
    assert not any(s == "failure" for (_, _, s, _) in rig.gh.statuses)


def test_closed_pr_drops_queued_check(rig: Rig) -> None:
    rig.pr("a" * 40, number=5)
    rig.pr("a" * 40, number=5, action="closed")
    rig.disp.drain_outbox()
    assert rig.states()["a" * 40] == "superseded"
    assert rig.gh.last("a" * 40)[0] == "success"


def test_claim_skips_check_whose_pr_head_moved(rig: Rig) -> None:
    rig.pr("a" * 40, number=5)
    rig.push("b" * 40)
    rig.gh.heads[(REPO, 5)] = d.PullHead("f" * 40, True)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None and job["sha"] == "b" * 40
    assert rig.states()["a" * 40] == "superseded"
    assert "fffffff" in rig.gh.last("a" * 40)[1]


def test_deploys_coalesce_to_newest_default_branch_sha(rig: Rig) -> None:
    rig.push("a" * 40)
    rig.push("b" * 40)
    rig.push("c" * 40)
    rig.push("d" * 40, repo=OTHER)
    rig.disp.drain_outbox()
    assert rig.states() == {"a" * 40: "superseded", "b" * 40: "superseded", "c" * 40: "queued", "d" * 40: "queued"}
    assert rig.gh.last("a" * 40)[0] == "success"


def test_leased_deploy_is_not_superseded_and_newer_deploy_waits(rig: Rig) -> None:
    rig.push("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    rig.push("b" * 40)
    assert rig.states() == {"a" * 40: "leased", "b" * 40: "queued"}


def test_duplicate_deliveries_and_repeat_events_do_not_run_a_job_twice(rig: Rig) -> None:
    assert rig.push("a" * 40, delivery="X") == (202, "queued build_deploy")
    assert rig.push("a" * 40, delivery="X") == (202, "duplicate delivery")
    assert rig.push("a" * 40, delivery="Y") == (202, "already queued build_deploy")
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    assert rig.push("a" * 40, delivery="Z") == (202, "already queued build_deploy")
    assert len([j for j in rig.disp.status()["recent"] if j["sha"] == "a" * 40]) == 1


def test_stale_lease_cannot_complete_or_heartbeat(rig: Rig) -> None:
    rig.pr("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    assert rig.disp.heartbeat(runner, job["id"], "wrong") == "lost"
    assert not rig.disp.complete(runner, job["id"], "wrong", "success")
    assert not rig.disp.complete("999", job["id"], job["lease"], "success")


def test_unknown_runner_gets_no_work(rig: Rig) -> None:
    rig.pr("a" * 40)
    rig.boot()
    with pytest.raises(d.UnknownRunner):
        rig.disp.claim("12345")
    assert rig.states()["a" * 40] == "queued"


def test_idle_box_is_destroyed_after_idle_window(rig: Rig) -> None:
    rig.pr("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    rig.clock.advance(rig.cfg.idle_seconds + 60)
    rig.disp.heartbeat(runner, job["id"], job["lease"])
    rig.tick()
    assert rig.cloud.deleted == []
    rig.disp.complete(runner, job["id"], job["lease"], "success")
    for _ in range(9):
        rig.clock.advance(60)
        rig.claim(runner)
        rig.tick()
    assert rig.cloud.deleted == []
    rig.clock.advance(120)
    rig.claim(runner)
    rig.tick()
    assert rig.cloud.deleted == [int(runner)]
    assert rig.disp.status()["box"] is None
    assert rig.cloud.droplets == {}


def test_new_work_after_teardown_creates_a_fresh_box(rig: Rig) -> None:
    rig.pr("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    rig.disp.complete(runner, job["id"], job["lease"], "success")
    rig.clock.advance(rig.cfg.idle_seconds + 1)
    rig.tick()
    rig.push("b" * 40)
    rig.tick()
    assert len(rig.cloud.created) == 2
    assert len(rig.cloud.droplets) == 1


def test_create_failure_retries_then_errors_queued_jobs(rig: Rig) -> None:
    rig.cloud.create_error = "HTTP 422 size unavailable"
    rig.pr("a" * 40)
    rig.tick()
    rig.tick()
    assert rig.states()["a" * 40] == "queued"
    rig.clock.advance(rig.cfg.create_retry_seconds)
    rig.tick()
    rig.tick()
    state, desc = rig.gh.last("a" * 40)
    assert state == "error" and "could not start CI box" in desc and "size unavailable" in desc
    assert rig.cloud.droplets == {}
    rig.cloud.create_error = None
    rig.pr("b" * 40, number=2)
    rig.clock.advance(rig.cfg.create_retry_seconds)
    rig.tick()
    assert len(rig.cloud.droplets) == 1


def test_missing_snapshot_is_a_visible_create_failure(rig: Rig) -> None:
    rig.cloud.snapshot = None
    rig.pr("a" * 40)
    for _ in range(3):
        rig.tick()
        rig.clock.advance(rig.cfg.create_retry_seconds)
    state, desc = rig.gh.last("a" * 40)
    assert state == "error" and "no snapshot" in desc


def test_box_that_never_boots_is_destroyed_and_counted_as_create_failure(rig: Rig) -> None:
    rig.pr("a" * 40)
    rig.tick()
    first = next(iter(rig.cloud.droplets))
    rig.clock.advance(rig.cfg.boot_timeout_seconds + 1)
    rig.tick()
    assert first in rig.cloud.deleted
    assert rig.disp.status()["create_failures"] == 1
    rig.clock.advance(rig.cfg.create_retry_seconds)
    rig.tick()
    assert len(rig.cloud.created) == 2
    rig.clock.advance(rig.cfg.boot_timeout_seconds + 1)
    rig.tick()
    rig.clock.advance(rig.cfg.create_retry_seconds)
    rig.tick()
    state, desc = rig.gh.last("a" * 40)
    assert state == "error" and "did not come up" in desc


def test_box_dying_mid_job_requeues_once_then_errors(rig: Rig) -> None:
    rig.push("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    rig.clock.advance(90)
    rig.cloud.droplets.clear()
    rig.tick()
    assert rig.states()["a" * 40] == "queued"
    assert rig.gh.last("a" * 40)[0] == "pending"
    assert "requeued" in rig.gh.last("a" * 40)[1]
    rig.clock.advance(60)
    runner2 = rig.boot()
    assert runner2 != runner
    job2 = rig.claim(runner2)
    assert job2 is not None and job2["id"] == job["id"] and job2["attempt"] == 2
    assert rig.disp.heartbeat(runner, job["id"], job["lease"]) == "lost"
    rig.clock.advance(90)
    rig.cloud.droplets.clear()
    rig.tick()
    state, desc = rig.gh.last("a" * 40)
    assert state == "error" and "lost mid-job" in desc


def test_silent_runner_loses_its_lease_and_box(rig: Rig) -> None:
    rig.pr("a" * 40)
    runner = rig.boot()
    assert rig.claim(runner) is not None
    rig.clock.advance(rig.cfg.lease_timeout_seconds + 1)
    rig.tick()
    assert rig.states()["a" * 40] == "queued"
    assert int(runner) in rig.cloud.deleted


def test_requeued_deploy_yields_to_newer_queued_deploy(rig: Rig) -> None:
    rig.push("a" * 40)
    runner = rig.boot()
    assert rig.claim(runner) is not None
    rig.push("b" * 40)
    rig.clock.advance(90)
    rig.cloud.droplets.clear()
    rig.tick()
    assert rig.states() == {"a" * 40: "superseded", "b" * 40: "queued"}


def test_job_timeout_posts_error(rig: Rig) -> None:
    rig.pr("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    for _ in range(int(rig.cfg.job_timeout_seconds // 60) + 1):
        rig.clock.advance(60)
        rig.disp.heartbeat(runner, job["id"], job["lease"])
    rig.tick()
    state, desc = rig.gh.last("a" * 40)
    assert state == "error" and "timed out" in desc
    assert rig.disp.heartbeat(runner, job["id"], job["lease"]) == "lost"


def test_queue_timeout_posts_error_when_no_box_ever_claims(rig: Rig) -> None:
    rig.cloud.list_error = "HTTP 503"
    rig.pr("a" * 40)
    rig.clock.advance(rig.cfg.queue_timeout_seconds + 1)
    rig.tick()
    state, desc = rig.gh.last("a" * 40)
    assert state == "error" and "not started" in desc


def test_list_failure_never_creates(rig: Rig) -> None:
    rig.cloud.list_error = "timeout"
    rig.pr("a" * 40)
    rig.tick()
    assert rig.cloud.created == []


def test_restart_adopts_existing_box_instead_of_creating_another(rig: Rig) -> None:
    rig.pr("a" * 40)
    rig.cloud.add(555, "platform-ci-ondemand-20260927-000000")
    rig.tick()
    assert rig.cloud.created == []
    assert rig.disp.status()["box"]["droplet_id"] == 555
    assert rig.claim("555") is not None


def test_restart_reconciliation_destroys_extra_boxes_but_never_foreign_ones(rig: Rig) -> None:
    rig.cloud.add(555, "platform-ci-ondemand-a")
    rig.cloud.add(556, "platform-ci-ondemand-b")
    rig.cloud.add(1, "platform")
    rig.pr("a" * 40)
    rig.tick()
    assert rig.disp.status()["box"]["droplet_id"] == 556
    assert rig.cloud.deleted == [555]
    assert 1 in rig.cloud.droplets
    assert rig.cloud.created == []


def test_idle_orphan_box_found_on_restart_is_torn_down(rig: Rig) -> None:
    rig.cloud.add(555, "platform-ci-ondemand-a")
    rig.clock.advance(rig.cfg.idle_seconds + 1)
    rig.tick()
    rig.clock.advance(rig.cfg.idle_seconds + 1)
    rig.tick()
    assert rig.cloud.droplets == {}


def test_delete_failure_is_retried(rig: Rig) -> None:
    rig.pr("a" * 40)
    runner = rig.boot()
    job = rig.claim(runner)
    assert job is not None
    rig.disp.complete(runner, job["id"], job["lease"], "success")
    rig.clock.advance(rig.cfg.idle_seconds + 1)
    original = rig.cloud.delete_droplet

    def broken(droplet_id: int) -> None:
        raise d.CloudError("HTTP 500")

    rig.cloud.delete_droplet = broken  # type: ignore[method-assign]
    rig.tick()
    assert int(runner) in rig.cloud.droplets
    rig.cloud.delete_droplet = original  # type: ignore[method-assign]
    rig.push("b" * 40)
    rig.tick()
    assert int(runner) not in rig.cloud.droplets
    rig.tick()
    assert len(rig.cloud.droplets) == 1


def test_hold_keeps_a_box_without_jobs(rig: Rig) -> None:
    rig.disp.hold(1800)
    runner = rig.boot()
    rig.clock.advance(rig.cfg.idle_seconds + 60)
    rig.claim(runner)
    rig.tick()
    assert rig.cloud.deleted == []
    rig.disp.hold(0)
    rig.clock.advance(rig.cfg.idle_seconds + 1)
    rig.claim(runner)
    rig.tick()
    assert rig.cloud.deleted == [int(runner)]


def test_outbox_retries_failed_status_posts_in_order(rig: Rig) -> None:
    rig.gh.fail = True
    rig.pr("a" * 40, number=1)
    rig.pr("b" * 40, number=1)
    assert rig.disp.drain_outbox() == 0
    rig.gh.fail = False
    assert rig.disp.drain_outbox() == 3
    assert [s for (_, x, s, _) in rig.gh.statuses if x == "a" * 40] == ["pending", "success"]


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_surface_verifies_hmac_and_tokens(rig: Rig) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), d.make_handler(rig.disp))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        body = json.dumps({"ref": "refs/heads/main", "after": "a" * 40,
                           "repository": {"full_name": REPO, "default_branch": "main"}}).encode()
        hdr = {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1", "Content-Type": "application/json"}
        assert _post(f"{base}/webhook", body, {**hdr, "X-Hub-Signature-256": "sha256=00"})[0] == 401
        assert _post(f"{base}/webhook", body, hdr)[0] == 401
        assert _post(f"{base}/webhook", body, {**hdr, "X-Hub-Signature-256": _sign(body)}) == (202, "queued build_deploy")
        claim = json.dumps({"runner_id": "1"}).encode()
        assert _post(f"{base}/runner/claim", claim, {"Authorization": "Bearer nope"})[0] == 401
        assert _post(f"{base}/runner/claim", claim, {"Authorization": "Bearer admin-token"})[0] == 401
        assert _post(f"{base}/runner/claim", claim, {"Authorization": "Bearer runner-token"})[0] == 403
        req = urllib.request.Request(f"{base}/status", headers={"Authorization": "Bearer runner-token"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=5)
        req = urllib.request.Request(f"{base}/status", headers={"Authorization": "Bearer admin-token"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert json.loads(resp.read())["queued"][0]["sha"] == "a" * 40
    finally:
        server.shutdown()
        server.server_close()


def test_check_only_repo_gets_pr_checks_but_no_build_on_push(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, check_only=frozenset({OTHER}))
    try:
        assert rig.push("a" * 40, repo=OTHER) == (202, "ignored push: repo is check-only")
        assert rig.pr("b" * 40, repo=OTHER)[0] == 202
        assert rig.push("c" * 40) == (202, "queued build_deploy")
        assert rig.states() == {"b" * 40: "queued", "c" * 40: "queued"}
    finally:
        rig.disp.close()


def test_config_from_env() -> None:
    cfg = d.Config.from_env({
        "PLATFORM_CI_REPO_MAP": json.dumps({REPO: {"project": "slopticus"}, OTHER: {"project": "human-index-v2", "build": False}}),
        "PLATFORM_CI_IDLE_MINUTES": "5",
        "PLATFORM_CI_BOX_SIZE": "s-8vcpu-16gb",
        "PLATFORM_CI_SSH_KEYS": "11, 22",
    })
    assert cfg.repo_map == {REPO: "slopticus", OTHER: "human-index-v2"}
    assert cfg.check_only == frozenset({OTHER})
    assert cfg.idle_seconds == 300
    assert cfg.size == "s-8vcpu-16gb"
    assert cfg.ssh_keys == ("11", "22")
    assert cfg.region == "nyc3"
