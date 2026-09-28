#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import worker

METADATA_ID_URL = "http://169.254.169.254/metadata/v1/id"


class UnknownRunner(Exception):
    pass


class DispatcherUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    dispatcher_url: str
    token: str
    runner_id: str
    poll_seconds: float = 5.0
    heartbeat_seconds: float = 30.0
    unknown_backoff_seconds: float = 30.0


class DispatcherClient:
    def __init__(self, cfg: RunnerConfig) -> None:
        self._cfg = cfg

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = json.dumps({"runner_id": self._cfg.runner_id, **body}).encode()
        req = urllib.request.Request(f"{self._cfg.dispatcher_url.rstrip('/')}{path}", data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self._cfg.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                parsed = json.loads(resp.read() or b"{}")
                return resp.status, parsed if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as e:
            if e.code in (403, 409):
                return e.code, {}
            raise DispatcherUnavailable(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise DispatcherUnavailable(str(e)) from e

    def claim(self) -> dict[str, Any] | None:
        code, data = self._post("/runner/claim", {})
        if code == 403:
            raise UnknownRunner(self._cfg.runner_id)
        job = data.get("job")
        return job if isinstance(job, dict) else None

    def heartbeat(self, job: dict[str, Any]) -> str:
        code, _ = self._post("/runner/heartbeat", {"job_id": job["id"], "lease": job["lease"]})
        return "ok" if code == 200 else "lost"

    def complete(self, job: dict[str, Any], outcome: str) -> bool:
        code, _ = self._post("/runner/complete", {"job_id": job["id"], "lease": job["lease"], "outcome": outcome})
        return code == 200


def execute(job: dict[str, Any]) -> str:
    worker.log(f"running {job['action']} for {job['repo']}@{str(job['sha'])[:7]} (job {job['id']})")
    action: Callable[[dict[str, Any]], str] = worker.do_check if job["action"] == "check" else worker.do_build_deploy
    try:
        return action(job)
    except Exception as e:
        worker.log(f"runner crash on job {job['id']}: {e!r}")
        worker.set_status(job["repo"], job["sha"], "error", f"platform-ci runner error: {e}")
        return "error"


def run_job(
    client: DispatcherClient,
    job: dict[str, Any],
    heartbeat_seconds: float,
    run: Callable[[dict[str, Any]], str] = execute,
) -> str:
    done = threading.Event()

    def beat() -> None:
        while not done.wait(heartbeat_seconds):
            try:
                if client.heartbeat(job) == "lost":
                    worker.log(f"lease lost for job {job['id']}; cancelling")
                    worker.cancel_running()
                    return
            except DispatcherUnavailable as e:
                worker.log(f"heartbeat failed (will retry): {e}")

    worker.begin_job()
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        outcome = run(job)
    finally:
        done.set()
        thread.join()
    if worker.is_cancelled():
        return "cancelled"
    return outcome


def report(client: DispatcherClient, job: dict[str, Any], outcome: str, retry_seconds: float,
           sleep: Callable[[float], None] = time.sleep) -> None:
    while True:
        try:
            accepted = client.complete(job, outcome)
            if not accepted:
                worker.log(f"dispatcher rejected completion of job {job['id']} (lease expired)")
            return
        except DispatcherUnavailable as e:
            worker.log(f"completion report failed (will retry): {e}")
            try:
                client.heartbeat(job)
            except DispatcherUnavailable:
                pass
            sleep(retry_seconds)


def serve(cfg: RunnerConfig, client: DispatcherClient, sleep: Callable[[float], None] = time.sleep,
          once: bool = False) -> None:
    while True:
        try:
            job = client.claim()
        except UnknownRunner:
            worker.log(f"dispatcher does not know runner {cfg.runner_id}; waiting")
            sleep(cfg.unknown_backoff_seconds)
            job = None
        except DispatcherUnavailable as e:
            worker.log(f"claim failed: {e}")
            sleep(cfg.poll_seconds)
            job = None
        else:
            if job is None:
                sleep(cfg.poll_seconds)
            else:
                outcome = run_job(client, job, cfg.heartbeat_seconds)
                if outcome != "cancelled":
                    report(client, job, outcome, cfg.poll_seconds, sleep)
        if once:
            return


def resolve_runner_id() -> str:
    explicit = os.environ.get("PLATFORM_CI_RUNNER_ID", "").strip()
    if explicit:
        return explicit
    with urllib.request.urlopen(METADATA_ID_URL, timeout=5) as resp:
        return str(resp.read().decode()).strip()


def main() -> int:
    url = os.environ.get("PLATFORM_CI_DISPATCHER_URL", "").strip()
    token = os.environ.get("PLATFORM_CI_RUNNER_TOKEN", "").strip()
    if not (url and token):
        worker.log("FATAL: PLATFORM_CI_DISPATCHER_URL and PLATFORM_CI_RUNNER_TOKEN are required")
        return 1
    os.makedirs(worker.LOG_DIR, exist_ok=True)
    cfg = RunnerConfig(dispatcher_url=url, token=token, runner_id=resolve_runner_id())
    worker.log(f"platform-ci runner {cfg.runner_id} pulling from {url}")
    serve(cfg, DispatcherClient(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
