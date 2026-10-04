#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

ACTIVE_STATES = ("queued", "leased")
LIVE_BOX_STATES = ("booting", "ready")
MAX_BODY_BYTES = 25 * 1024 * 1024


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


def short(sha: str) -> str:
    return sha[:7]


@dataclass(frozen=True)
class Config:
    webhook_secret: str
    runner_token: str
    admin_token: str
    repo_map: dict[str, str]
    db_path: str
    check_only: frozenset[str] = frozenset()
    tag: str = "platform-ci-ondemand"
    name_prefix: str = "platform-ci-ondemand"
    snapshot_prefix: str = "platform-ci-snap-"
    region: str = "nyc3"
    size: str = "s-4vcpu-8gb-intel"
    ssh_keys: tuple[str, ...] = ()
    status_context: str = "platform-ci"
    idle_seconds: float = 600.0
    boot_timeout_seconds: float = 600.0
    lease_timeout_seconds: float = 180.0
    job_timeout_seconds: float = 5400.0
    queue_timeout_seconds: float = 3600.0
    create_retry_seconds: float = 60.0
    create_max_failures: int = 2
    max_attempts: int = 2
    listen_host: str = "0.0.0.0"
    listen_port: int = 8766
    tick_seconds: float = 15.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        raw_map = json.loads(env.get("PLATFORM_CI_REPO_MAP", "{}") or "{}")
        repo_map = {repo: str(entry["project"]) for repo, entry in raw_map.items()}
        check_only = frozenset(repo for repo, entry in raw_map.items() if entry.get("build") is False)
        keys = tuple(k.strip() for k in env.get("PLATFORM_CI_SSH_KEYS", "").split(",") if k.strip())

        def num(name: str, default: float) -> float:
            value = env.get(name, "").strip()
            return float(value) if value else default

        return cls(
            webhook_secret=env.get("PLATFORM_CI_WEBHOOK_SECRET", ""),
            runner_token=env.get("PLATFORM_CI_RUNNER_TOKEN", ""),
            admin_token=env.get("PLATFORM_CI_ADMIN_TOKEN", ""),
            repo_map=repo_map,
            check_only=check_only,
            db_path=env.get("PLATFORM_CI_DB", "/data/dispatcher.sqlite"),
            tag=env.get("PLATFORM_CI_BOX_TAG", cls.tag),
            name_prefix=env.get("PLATFORM_CI_BOX_NAME_PREFIX", cls.name_prefix),
            snapshot_prefix=env.get("PLATFORM_CI_SNAPSHOT_PREFIX", cls.snapshot_prefix),
            region=env.get("PLATFORM_CI_BOX_REGION", cls.region),
            size=env.get("PLATFORM_CI_BOX_SIZE", cls.size),
            ssh_keys=keys,
            status_context=env.get("PLATFORM_CI_STATUS_CONTEXT", cls.status_context),
            idle_seconds=num("PLATFORM_CI_IDLE_MINUTES", cls.idle_seconds / 60) * 60,
            boot_timeout_seconds=num("PLATFORM_CI_BOOT_TIMEOUT_MINUTES", cls.boot_timeout_seconds / 60) * 60,
            lease_timeout_seconds=num("PLATFORM_CI_LEASE_TIMEOUT_SECONDS", cls.lease_timeout_seconds),
            job_timeout_seconds=num("PLATFORM_CI_JOB_TIMEOUT_MINUTES", cls.job_timeout_seconds / 60) * 60,
            queue_timeout_seconds=num("PLATFORM_CI_QUEUE_TIMEOUT_MINUTES", cls.queue_timeout_seconds / 60) * 60,
            create_retry_seconds=num("PLATFORM_CI_CREATE_RETRY_SECONDS", cls.create_retry_seconds),
            create_max_failures=int(num("PLATFORM_CI_CREATE_MAX_FAILURES", cls.create_max_failures)),
            listen_host=env.get("PLATFORM_CI_HOST", cls.listen_host),
            listen_port=int(num("PLATFORM_CI_PORT", cls.listen_port)),
        )


@dataclass(frozen=True)
class Droplet:
    id: int
    name: str
    status: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    id: int
    name: str
    created_at: str


@dataclass(frozen=True)
class DropletSpec:
    name: str
    region: str
    size: str
    image: int
    ssh_keys: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class PullHead:
    sha: str
    open: bool


class CloudError(Exception):
    pass


class UnknownRunner(Exception):
    pass


class Cloud(Protocol):
    def list_droplets(self, tag: str) -> list[Droplet]: ...

    def latest_snapshot(self, prefix: str) -> Snapshot | None: ...

    def create_droplet(self, spec: DropletSpec) -> Droplet: ...

    def delete_droplet(self, droplet_id: int) -> None: ...


class GitHub(Protocol):
    def set_status(self, repo: str, sha: str, state: str, description: str, context: str) -> bool: ...

    def pr_head(self, repo: str, number: int) -> PullHead | None: ...


class DigitalOcean:
    def __init__(self, token: str, base: str = "https://api.digitalocean.com/v2") -> None:
        self._token = token
        self._base = base

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self._base}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise CloudError(f"DO {method} {path.split('?')[0]}: HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CloudError(f"DO {method} {path.split('?')[0]}: {e}") from e
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _droplet(raw: dict[str, Any]) -> Droplet:
        return Droplet(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            status=str(raw.get("status", "")),
            tags=tuple(str(t) for t in raw.get("tags") or ()),
        )

    def list_droplets(self, tag: str) -> list[Droplet]:
        data = self._call("GET", f"/droplets?tag_name={urllib.parse.quote(tag)}&per_page=200")
        return [self._droplet(d) for d in data.get("droplets") or []]

    def latest_snapshot(self, prefix: str) -> Snapshot | None:
        data = self._call("GET", "/images?private=true&per_page=200")
        found = [
            Snapshot(id=int(i["id"]), name=str(i.get("name", "")), created_at=str(i.get("created_at", "")))
            for i in data.get("images") or []
            if str(i.get("name", "")).startswith(prefix) and i.get("status", "available") == "available"
        ]
        return max(found, key=lambda s: s.created_at) if found else None

    def create_droplet(self, spec: DropletSpec) -> Droplet:
        body: dict[str, Any] = {
            "name": spec.name,
            "region": spec.region,
            "size": spec.size,
            "image": spec.image,
            "ssh_keys": list(spec.ssh_keys),
            "tags": list(spec.tags),
            "monitoring": False,
            "ipv6": False,
        }
        return self._droplet(self._call("POST", "/droplets", body)["droplet"])

    def delete_droplet(self, droplet_id: int) -> None:
        self._call("DELETE", f"/droplets/{int(droplet_id)}")


class GitHubApi:
    def __init__(self, token: str) -> None:
        self._token = token

    def _call(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "platform-ci-dispatcher")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                parsed = json.loads(resp.read() or b"{}")
                return resp.status, parsed if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return 0, {}

    def set_status(self, repo: str, sha: str, state: str, description: str, context: str) -> bool:
        body = {"state": state, "context": context, "description": description[:140]}
        code, _ = self._call("POST", f"https://api.github.com/repos/{repo}/statuses/{sha}", body)
        log(f"commit status {state} for {repo}@{short(sha)}: HTTP {code}")
        return 200 <= code < 300

    def pr_head(self, repo: str, number: int) -> PullHead | None:
        code, data = self._call("GET", f"https://api.github.com/repos/{repo}/pulls/{int(number)}")
        if code != 200:
            return None
        return PullHead(sha=str((data.get("head") or {}).get("sha", "")), open=data.get("state") == "open")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    repo TEXT NOT NULL,
    project TEXT NOT NULL,
    sha TEXT NOT NULL,
    pr INTEGER,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease TEXT,
    runner TEXT,
    created_at REAL NOT NULL,
    claimed_at REAL,
    heartbeat_at REAL,
    finished_at REAL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS boxes (
    droplet_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    seen_at REAL,
    ended_at REAL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    sha TEXT NOT NULL,
    state TEXT NOT NULL,
    description TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Job:
    id: int
    action: str
    repo: str
    project: str
    sha: str
    pr: int | None
    state: str
    attempts: int
    lease: str | None
    runner: str | None
    created_at: float
    claimed_at: float | None
    heartbeat_at: float | None
    detail: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "repo": self.repo,
            "project": self.project,
            "sha": self.sha,
            "pr": self.pr,
            "state": self.state,
            "attempts": self.attempts,
            "runner": self.runner,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "detail": self.detail,
        }


@dataclass
class Box:
    droplet_id: int
    name: str
    state: str
    created_at: float
    seen_at: float | None


@dataclass
class Dispatcher:
    cfg: Config
    cloud: Cloud
    github: GitHub
    clock: Callable[[], float] = time.time
    _db: sqlite3.Connection = field(init=False)
    _lock: threading.RLock = field(init=False, default_factory=threading.RLock)
    _outbox_lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.cfg.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.cfg.db_path)), exist_ok=True)
        self._db = sqlite3.connect(self.cfg.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _touch(self) -> None:
        self._set_meta("activity_at", repr(self.clock()))

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=int(row["id"]),
            action=str(row["action"]),
            repo=str(row["repo"]),
            project=str(row["project"]),
            sha=str(row["sha"]),
            pr=None if row["pr"] is None else int(row["pr"]),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            lease=row["lease"],
            runner=row["runner"],
            created_at=float(row["created_at"]),
            claimed_at=row["claimed_at"],
            heartbeat_at=row["heartbeat_at"],
            detail=str(row["detail"]),
        )

    def _jobs(self, where: str, params: tuple[Any, ...] = ()) -> list[Job]:
        rows = self._db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY id", params).fetchall()
        return [self._job(r) for r in rows]

    def _status(self, job: Job, state: str, description: str) -> None:
        self._db.execute(
            "INSERT INTO outbox(repo, sha, state, description) VALUES (?, ?, ?, ?)",
            (job.repo, job.sha, state, f"platform-ci: {description}"[:140]),
        )

    def _finish(self, job: Job, state: str, detail: str) -> None:
        self._db.execute(
            "UPDATE jobs SET state = ?, lease = NULL, finished_at = ?, detail = ? WHERE id = ?",
            (state, self.clock(), detail, job.id),
        )

    def _supersede(self, job: Job, by: str) -> None:
        self._finish(job, "superseded", by)
        self._status(job, "success", f"superseded: {by}; not run")
        log(f"superseded {job.action} {job.repo}@{short(job.sha)} ({by})")

    def _fail(self, job: Job, description: str) -> None:
        self._finish(job, "error", description)
        self._status(job, "error", description)
        log(f"error {job.action} {job.repo}@{short(job.sha)}: {description}")

    def _live_box(self) -> Box | None:
        row = self._db.execute(
            "SELECT * FROM boxes WHERE state IN (?, ?) ORDER BY created_at DESC LIMIT 1", LIVE_BOX_STATES
        ).fetchone()
        if row is None:
            return None
        return Box(
            droplet_id=int(row["droplet_id"]),
            name=str(row["name"]),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            seen_at=row["seen_at"],
        )

    def handle_webhook(self, event: str, delivery: str, payload: dict[str, Any]) -> tuple[int, str]:
        if event == "ping":
            return 200, "pong"
        repo = str((payload.get("repository") or {}).get("full_name", ""))
        project = self.cfg.repo_map.get(repo)
        if not project:
            return 202, f"ignored: {repo} not registered"
        with self._tx() as db:
            if delivery:
                if db.execute("SELECT 1 FROM deliveries WHERE id = ?", (delivery,)).fetchone():
                    return 202, "duplicate delivery"
                db.execute("INSERT INTO deliveries(id, received_at) VALUES (?, ?)", (delivery, self.clock()))
            if event == "pull_request":
                return self._on_pull_request(repo, project, payload)
            if event == "push":
                return self._on_push(repo, project, payload)
        return 202, f"ignored event {event}"

    def _on_pull_request(self, repo: str, project: str, payload: dict[str, Any]) -> tuple[int, str]:
        pr = payload.get("pull_request") or {}
        number = int(pr.get("number") or payload.get("number") or 0)
        action = payload.get("action")
        if action == "closed":
            for job in self._jobs("state = 'queued' AND action = 'check' AND repo = ? AND pr = ?", (repo, number)):
                self._supersede(job, f"PR #{number} closed")
            return 202, "closed"
        if action not in {"opened", "synchronize", "reopened"}:
            return 202, "ignored pr action"
        sha = str((pr.get("head") or {}).get("sha", ""))
        if not sha:
            return 202, "skipped"
        return self._enqueue("check", repo, project, sha, number)

    def _on_push(self, repo: str, project: str, payload: dict[str, Any]) -> tuple[int, str]:
        default_branch = (payload.get("repository") or {}).get("default_branch", "main")
        if payload.get("ref") != f"refs/heads/{default_branch}":
            return 202, "ignored non-default branch"
        if payload.get("deleted"):
            return 202, "ignored branch delete"
        if repo in self.cfg.check_only:
            return 202, "ignored push: repo is check-only"
        sha = str(payload.get("after", ""))
        if not sha or sha == "0" * 40:
            return 202, "skipped"
        return self._enqueue("build_deploy", repo, project, sha, None)

    def _enqueue(self, action: str, repo: str, project: str, sha: str, pr: int | None) -> tuple[int, str]:
        if self._jobs("state IN ('queued', 'leased') AND action = ? AND repo = ? AND sha = ?", (action, repo, sha)):
            return 202, f"already queued {action}"
        now = self.clock()
        cur = self._db.execute(
            "INSERT INTO jobs(action, repo, project, sha, pr, state, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (action, repo, project, sha, pr, now),
        )
        job = self._jobs("id = ?", (int(cur.lastrowid or 0),))[0]
        if action == "check":
            for old in self._jobs(
                "state = 'queued' AND action = 'check' AND repo = ? AND pr = ? AND id != ?", (repo, pr, job.id)
            ):
                self._supersede(old, f"PR #{pr} head moved to {short(sha)}")
        else:
            for old in self._jobs(
                "state = 'queued' AND action = 'build_deploy' AND project = ? AND id != ?", (project, job.id)
            ):
                self._supersede(old, f"newer {project} deploy {short(sha)} queued")
        depth = len(self._jobs("state = 'queued'"))
        self._status(job, "pending", f"queued ({depth} in queue)")
        self._touch()
        log(f"queued {action} {repo}@{short(sha)} (queue depth {depth})")
        return 202, f"queued {action}"

    def claim(self, runner_id: str) -> dict[str, Any] | None:
        while True:
            with self._tx() as db:
                box = self._live_box()
                if box is None or str(box.droplet_id) != runner_id:
                    raise UnknownRunner(runner_id)
                now = self.clock()
                if box.state == "booting":
                    log(f"box {box.name} ({box.droplet_id}) is up")
                    self._set_meta("create_failures", "0")
                db.execute(
                    "UPDATE boxes SET state = 'ready', seen_at = ? WHERE droplet_id = ?", (now, box.droplet_id)
                )
                for orphan in self._jobs("state = 'leased' AND runner = ?", (runner_id,)):
                    self._lose(orphan, "runner restarted mid-job")
                deploy_running = bool(self._jobs("state = 'leased' AND action = 'build_deploy'"))
                candidate = next(
                    (
                        j
                        for j in self._jobs("state = 'queued'")
                        if not (j.action == "build_deploy" and deploy_running)
                    ),
                    None,
                )
            if candidate is None:
                return None
            head = (
                self.github.pr_head(candidate.repo, candidate.pr)
                if candidate.action == "check" and candidate.pr
                else None
            )
            with self._tx() as db:
                current = self._jobs("id = ? AND state = 'queued'", (candidate.id,))
                if not current:
                    continue
                job = current[0]
                if job.action == "build_deploy" and self._jobs("state = 'leased' AND action = 'build_deploy'"):
                    return None
                if head is not None and not head.open:
                    self._supersede(job, f"PR #{job.pr} is closed")
                    continue
                if head is not None and head.sha and head.sha != job.sha:
                    self._supersede(job, f"PR #{job.pr} head is now {short(head.sha)}")
                    continue
                lease = secrets.token_hex(16)
                now = self.clock()
                db.execute(
                    "UPDATE jobs SET state = 'leased', lease = ?, runner = ?, attempts = attempts + 1, "
                    "claimed_at = ?, heartbeat_at = ? WHERE id = ?",
                    (lease, runner_id, now, now, job.id),
                )
                self._touch()
                log(f"leased {job.action} {job.repo}@{short(job.sha)} to {runner_id} (attempt {job.attempts + 1})")
                return {
                    "id": job.id,
                    "lease": lease,
                    "action": job.action,
                    "repo": job.repo,
                    "project": job.project,
                    "sha": job.sha,
                    "attempt": job.attempts + 1,
                }

    def heartbeat(self, runner_id: str, job_id: int, lease: str) -> str:
        with self._tx() as db:
            now = self.clock()
            db.execute("UPDATE boxes SET seen_at = ? WHERE droplet_id = ? AND state IN ('booting', 'ready')",
                       (now, int(runner_id) if runner_id.isdigit() else -1))
            if not self._jobs("id = ? AND state = 'leased' AND lease = ? AND runner = ?", (job_id, lease, runner_id)):
                return "lost"
            db.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (now, job_id))
            self._touch()
            return "ok"

    def complete(self, runner_id: str, job_id: int, lease: str, outcome: str) -> bool:
        with self._tx():
            found = self._jobs("id = ? AND state = 'leased' AND lease = ? AND runner = ?", (job_id, lease, runner_id))
            if not found:
                return False
            self._finish(found[0], "done", outcome)
            self._touch()
            log(f"done {found[0].action} {found[0].repo}@{short(found[0].sha)}: {outcome}")
            return True

    def hold(self, seconds: float) -> float:
        with self._tx():
            until = self.clock() + max(0.0, seconds)
            self._set_meta("hold_until", repr(until))
            self._touch()
            return until

    def recover(self) -> None:
        with self._tx() as db:
            now = self.clock()
            db.execute("UPDATE jobs SET heartbeat_at = ? WHERE state = 'leased'", (now,))
            db.execute("UPDATE boxes SET seen_at = ? WHERE state = 'ready'", (now,))
            self._touch()

    def _lose(self, job: Job, why: str) -> None:
        if job.attempts >= self.cfg.max_attempts:
            self._fail(job, f"CI box lost mid-job {job.attempts}x ({why}); giving up")
            return
        if job.action == "build_deploy" and self._jobs(
            "state = 'queued' AND action = 'build_deploy' AND project = ? AND id > ?", (job.project, job.id)
        ):
            self._supersede(job, "newer deploy queued")
            return
        self._db.execute(
            "UPDATE jobs SET state = 'queued', lease = NULL, runner = NULL, detail = ? WHERE id = ?",
            (f"requeued: {why}", job.id),
        )
        self._status(job, "pending", f"requeued after {why}")
        log(f"requeued {job.action} {job.repo}@{short(job.sha)}: {why}")

    def _demand(self) -> bool:
        if self._jobs("state IN ('queued', 'leased')"):
            return True
        return float(self._meta("hold_until", "0") or 0) > self.clock()

    def _expire(self) -> None:
        now = self.clock()
        for job in self._jobs("state = 'leased'"):
            if job.claimed_at is not None and now - job.claimed_at > self.cfg.job_timeout_seconds:
                self._fail(job, f"timed out after {int(self.cfg.job_timeout_seconds // 60)} min")
            elif job.heartbeat_at is None or now - job.heartbeat_at > self.cfg.lease_timeout_seconds:
                self._lose(job, "runner stopped heartbeating")
        for job in self._jobs("state = 'queued'"):
            if now - job.created_at > self.cfg.queue_timeout_seconds:
                self._fail(job, f"not started within {int(self.cfg.queue_timeout_seconds // 60)} min (no CI box)")

    def _box_gone(self, box: Box, why: str) -> None:
        self._db.execute(
            "UPDATE boxes SET state = 'gone', ended_at = ?, detail = ? WHERE droplet_id = ?",
            (self.clock(), why, box.droplet_id),
        )
        for job in self._jobs("state = 'leased' AND runner = ?", (str(box.droplet_id),)):
            self._lose(job, why)
        log(f"box {box.name} ({box.droplet_id}) gone: {why}")

    def _mark_destroying(self, box: Box, why: str) -> None:
        self._db.execute(
            "UPDATE boxes SET state = 'destroying', detail = ? WHERE droplet_id = ?", (why, box.droplet_id)
        )
        for job in self._jobs("state = 'leased' AND runner = ?", (str(box.droplet_id),)):
            self._lose(job, why)
        log(f"destroying box {box.name} ({box.droplet_id}): {why}")

    def _create_failed(self, why: str) -> None:
        failures = int(self._meta("create_failures", "0") or 0) + 1
        self._set_meta("create_failures", str(failures))
        self._set_meta("last_create_error", why)
        log(f"CI box create failure {failures}/{self.cfg.create_max_failures}: {why}")

    def tick(self) -> None:
        with self._tx() as db:
            self._expire()
            db.execute("DELETE FROM deliveries WHERE received_at < ?", (self.clock() - 14 * 86400,))
        try:
            droplets = self.cloud.list_droplets(self.cfg.tag)
        except CloudError as e:
            log(f"cannot list CI droplets: {e}")
            return
        foreign = [d for d in droplets if not d.name.startswith(f"{self.cfg.name_prefix}-")]
        for d in foreign:
            log(f"ignoring tagged droplet {d.name} ({d.id}): name lacks prefix {self.cfg.name_prefix}-")
        droplets = [d for d in droplets if d not in foreign]
        by_id = {d.id: d for d in droplets}
        to_delete: list[int] = []
        create = False
        with self._tx() as db:
            now = self.clock()
            box = self._live_box()
            for row in db.execute("SELECT droplet_id FROM boxes WHERE state = 'destroying'").fetchall():
                did = int(row["droplet_id"])
                if did in by_id:
                    to_delete.append(did)
                else:
                    db.execute(
                        "UPDATE boxes SET state = 'gone', ended_at = ? WHERE droplet_id = ?", (now, did)
                    )
            if box is not None and box.droplet_id not in by_id and now - box.created_at > 60:
                self._box_gone(box, "droplet no longer exists")
                box = None
            if box is None:
                candidates = [d for d in droplets if d.id not in to_delete]
                if candidates:
                    keep = max(candidates, key=lambda d: d.id)
                    db.execute(
                        "INSERT OR REPLACE INTO boxes(droplet_id, name, state, created_at) VALUES (?, ?, 'booting', ?)",
                        (keep.id, keep.name, now),
                    )
                    log(f"adopted existing CI droplet {keep.name} ({keep.id})")
                    box = self._live_box()
            if box is not None:
                for d in droplets:
                    if d.id != box.droplet_id and d.id not in to_delete:
                        db.execute(
                            "INSERT OR REPLACE INTO boxes(droplet_id, name, state, created_at, detail) "
                            "VALUES (?, ?, 'destroying', ?, 'extra droplet')",
                            (d.id, d.name, now),
                        )
                        to_delete.append(d.id)
            demand = self._demand()
            activity = float(self._meta("activity_at", "0") or 0)
            if box is not None:
                if box.state == "booting" and now - box.created_at > self.cfg.boot_timeout_seconds:
                    self._mark_destroying(box, "box did not come up in time")
                    self._create_failed(f"box did not come up within {int(self.cfg.boot_timeout_seconds // 60)} min")
                    to_delete.append(box.droplet_id)
                elif box.state == "ready" and (box.seen_at is None or now - box.seen_at > self.cfg.lease_timeout_seconds):
                    self._mark_destroying(box, "runner stopped polling")
                    to_delete.append(box.droplet_id)
                elif not demand and now - activity > self.cfg.idle_seconds:
                    self._mark_destroying(box, f"idle {int(self.cfg.idle_seconds // 60)} min")
                    to_delete.append(box.droplet_id)
            elif demand and not droplets:
                failures = int(self._meta("create_failures", "0") or 0)
                last_attempt = float(self._meta("last_create_attempt", "0") or 0)
                if failures >= self.cfg.create_max_failures:
                    why = self._meta("last_create_error", "unknown error")
                    for job in self._jobs("state = 'queued'"):
                        self._fail(job, f"could not start CI box: {why}")
                    self._set_meta("create_failures", "0")
                    self._set_meta("hold_until", "0")
                elif now - last_attempt >= self.cfg.create_retry_seconds:
                    self._set_meta("last_create_attempt", repr(now))
                    create = True
        for did in dict.fromkeys(to_delete):
            try:
                self.cloud.delete_droplet(did)
                with self._tx() as db:
                    db.execute(
                        "UPDATE boxes SET state = 'gone', ended_at = ? WHERE droplet_id = ?", (self.clock(), did)
                    )
                log(f"deleted CI droplet {did}")
            except CloudError as e:
                log(f"delete of CI droplet {did} failed (will retry): {e}")
        if create:
            self._create_box()

    def _create_box(self) -> None:
        try:
            snap = self.cloud.latest_snapshot(self.cfg.snapshot_prefix)
            if snap is None:
                raise CloudError(f"no snapshot named {self.cfg.snapshot_prefix}*")
            spec = DropletSpec(
                name=f"{self.cfg.name_prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(self.clock()))}",
                region=self.cfg.region,
                size=self.cfg.size,
                image=snap.id,
                ssh_keys=self.cfg.ssh_keys,
                tags=(self.cfg.tag,),
            )
            droplet = self.cloud.create_droplet(spec)
        except CloudError as e:
            with self._tx():
                self._create_failed(str(e)[:200])
            return
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO boxes(droplet_id, name, state, created_at) VALUES (?, ?, 'booting', ?)",
                (droplet.id, droplet.name, self.clock()),
            )
            self._touch()
        log(f"created CI droplet {droplet.name} ({droplet.id}) from {snap.name} size={self.cfg.size}")

    def drain_outbox(self) -> int:
        sent = 0
        with self._outbox_lock:
            while True:
                with self._lock:
                    row = self._db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    return sent
                ok = self.github.set_status(
                    str(row["repo"]), str(row["sha"]), str(row["state"]), str(row["description"]),
                    self.cfg.status_context,
                )
                with self._tx() as db:
                    if ok or int(row["tries"]) + 1 >= 5:
                        db.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
                        sent += 1 if ok else 0
                    else:
                        db.execute("UPDATE outbox SET tries = tries + 1 WHERE id = ?", (row["id"],))
                        return sent

    def status(self) -> dict[str, Any]:
        with self._lock:
            box = self._live_box()
            recent = self._db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 20").fetchall()
            return {
                "now": self.clock(),
                "box": None if box is None else {
                    "droplet_id": box.droplet_id,
                    "name": box.name,
                    "state": box.state,
                    "created_at": box.created_at,
                    "seen_at": box.seen_at,
                },
                "queued": [j.public() for j in self._jobs("state = 'queued'")],
                "running": [j.public() for j in self._jobs("state = 'leased'")],
                "recent": [self._job(r).public() for r in recent],
                "activity_at": float(self._meta("activity_at", "0") or 0),
                "hold_until": float(self._meta("hold_until", "0") or 0),
                "create_failures": int(self._meta("create_failures", "0") or 0),
                "last_create_error": self._meta("last_create_error", ""),
                "outbox": int(self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]),
                "config": {
                    "size": self.cfg.size,
                    "region": self.cfg.region,
                    "idle_minutes": self.cfg.idle_seconds / 60,
                    "snapshot_prefix": self.cfg.snapshot_prefix,
                },
            }


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def bearer_ok(expected: str, header: str) -> bool:
    return bool(expected) and hmac.compare_digest(f"Bearer {expected}".encode(), header.encode())


def make_handler(dispatcher: Dispatcher) -> type[BaseHTTPRequestHandler]:
    cfg = dispatcher.cfg

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _reply(self, code: int, body: str | dict[str, Any] | None) -> None:
            raw = b"" if body is None else (json.dumps(body) if isinstance(body, dict) else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if isinstance(body, dict) else "text/plain")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > MAX_BODY_BYTES:
                self._reply(413, "too large")
                return None
            return self.rfile.read(length)

        def _json(self, raw: bytes) -> dict[str, Any] | None:
            try:
                parsed = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._reply(200, "ok")
            elif self.path == "/status":
                if not bearer_ok(cfg.admin_token, self.headers.get("Authorization", "")):
                    self._reply(401, "unauthorized")
                    return
                self._reply(200, dispatcher.status())
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            body = self._body()
            if body is None:
                return
            if self.path == "/webhook":
                self._webhook(body)
            elif self.path.startswith("/runner/"):
                self._runner(body)
            elif self.path == "/admin/hold":
                if not bearer_ok(cfg.admin_token, self.headers.get("Authorization", "")):
                    self._reply(401, "unauthorized")
                    return
                data = self._json(body) or {}
                until = dispatcher.hold(float(data.get("minutes", 0)) * 60)
                self._reply(200, {"hold_until": until})
            else:
                self._reply(404, "not found")

        def _webhook(self, body: bytes) -> None:
            if not verify_signature(cfg.webhook_secret, body, self.headers.get("X-Hub-Signature-256", "")):
                log("rejected delivery: bad/missing HMAC signature")
                self._reply(401, "bad signature")
                return
            payload = self._json(body)
            if payload is None:
                self._reply(400, "bad json")
                return
            code, msg = dispatcher.handle_webhook(
                self.headers.get("X-GitHub-Event", ""), self.headers.get("X-GitHub-Delivery", ""), payload
            )
            self._reply(code, msg)

        def _runner(self, body: bytes) -> None:
            if not bearer_ok(cfg.runner_token, self.headers.get("Authorization", "")):
                self._reply(401, "unauthorized")
                return
            data = self._json(body)
            if data is None:
                self._reply(400, "bad json")
                return
            runner_id = str(data.get("runner_id", ""))
            if self.path == "/runner/claim":
                try:
                    job = dispatcher.claim(runner_id)
                except UnknownRunner:
                    self._reply(403, "unknown runner")
                    return
                self._reply(200, {"job": job})
            elif self.path == "/runner/heartbeat":
                state = dispatcher.heartbeat(runner_id, int(data.get("job_id", 0)), str(data.get("lease", "")))
                self._reply(200 if state == "ok" else 409, {"state": state})
            elif self.path == "/runner/complete":
                ok = dispatcher.complete(
                    runner_id, int(data.get("job_id", 0)), str(data.get("lease", "")), str(data.get("outcome", ""))
                )
                self._reply(200 if ok else 409, {"accepted": ok})
            else:
                self._reply(404, "not found")

    return Handler


def run_forever(interval: float, fn: Callable[[], object], name: str) -> None:
    while True:
        try:
            fn()
        except Exception as e:
            log(f"{name} loop error: {e!r}")
        time.sleep(interval)


def main() -> int:
    cfg = Config.from_env(os.environ)
    do_token = os.environ.get("PLATFORM_CI_DO_TOKEN", "")
    gh_token = os.environ.get("PLATFORM_CI_GH_TOKEN", "")
    missing = [
        name
        for name, value in (
            ("PLATFORM_CI_WEBHOOK_SECRET", cfg.webhook_secret),
            ("PLATFORM_CI_RUNNER_TOKEN", cfg.runner_token),
            ("PLATFORM_CI_ADMIN_TOKEN", cfg.admin_token),
            ("PLATFORM_CI_DO_TOKEN", do_token),
            ("PLATFORM_CI_GH_TOKEN", gh_token),
        )
        if not value
    ]
    if missing:
        log(f"FATAL: missing {', '.join(missing)}")
        return 1
    cloud = DigitalOcean(do_token, os.environ.get("PLATFORM_CI_DO_API_URL", "https://api.digitalocean.com/v2"))
    dispatcher = Dispatcher(cfg=cfg, cloud=cloud, github=GitHubApi(gh_token))
    dispatcher.recover()
    threading.Thread(target=run_forever, args=(cfg.tick_seconds, dispatcher.tick, "tick"), daemon=True).start()
    threading.Thread(target=run_forever, args=(2.0, dispatcher.drain_outbox, "outbox"), daemon=True).start()
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), make_handler(dispatcher))
    log(
        f"platform-ci dispatcher on {cfg.listen_host}:{cfg.listen_port}; repos={sorted(cfg.repo_map)}; "
        f"box={cfg.size}/{cfg.region} tag={cfg.tag} idle={cfg.idle_seconds / 60:g}m"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
