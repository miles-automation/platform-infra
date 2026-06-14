#!/usr/bin/env python3
"""platform-ci worker — the GitHub-Actions-free CI/CD trigger (docs/cicd-redesign.md §12).

A tiny stdlib HTTP service (no FastAPI/uvicorn to maintain on an unattended box) that receives
HMAC-verified GitHub webhooks and runs the matching `bin/platform` action on this droplet, then
reports a GitHub commit status so PRs still gate. The box IS linux/amd64, so image builds are
native — none of the QEMU cross-build hang that broke the manual deploy.

Events handled:
  - ping                         -> 200 (webhook registration check)
  - pull_request (opened/sync/reopened) -> `check`: make check at the PR head SHA -> commit status
  - push to refs/heads/<default> -> `build` + `deploy`: bin/platform build <proj> --rollout

Design choices:
  - Webhook returns 202 immediately; the action runs on a single serialized worker thread (small
    box, one build at a time). A second delivery while busy is queued (bounded), newest-per-repo.
  - Every secret (HMAC, GitHub token, ghcr/prod creds) comes from the environment (systemd
    EnvironmentFile), never this file.
  - The repo->project map and the workspace path are config; adding a repo is one env entry.
"""

import hashlib
import hmac
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- config (from env)

WORKSPACE = os.environ.get("PLATFORM_CI_WORKSPACE", "/srv/platform-ci/workspace")
PLATFORM_BIN = os.path.join(WORKSPACE, "repos/platform-infra/bin/platform")
REPOS_DIR = os.path.join(WORKSPACE, "repos")
WEBHOOK_SECRET = os.environ.get("PLATFORM_CI_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
LISTEN_HOST = os.environ.get("PLATFORM_CI_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PLATFORM_CI_PORT", "8765"))
STATUS_CONTEXT = os.environ.get("PLATFORM_CI_STATUS_CONTEXT", "platform-ci")
LOG_DIR = os.environ.get("PLATFORM_CI_LOG_DIR", "/srv/platform-ci/logs")

# repo full_name -> {project, clone_url}. JSON in PLATFORM_CI_REPO_MAP, e.g.
# {"miles-automation/human-index-v2": {"project": "human-index-v2"}}
try:
    REPO_MAP = json.loads(os.environ.get("PLATFORM_CI_REPO_MAP", "{}"))
except json.JSONDecodeError:
    REPO_MAP = {}

# Actions that actually deploy to prod. Build+deploy on push is powerful; keep it opt-in per repo
# so adding a repo for `check` only doesn't silently start auto-deploying it.
DEPLOY_ON_PUSH = set(
    s.strip() for s in os.environ.get("PLATFORM_CI_DEPLOY_ON_PUSH", "").split(",") if s.strip()
)

_jobs: "queue.Queue[dict]" = queue.Queue(maxsize=64)


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


# --------------------------------------------------------------------------- GitHub helpers


def _gh_api(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "platform-ci")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}
    except Exception as e:  # noqa: BLE001 — never let status reporting crash the worker
        return 0, {"error": str(e)}


def set_status(repo: str, sha: str, state: str, description: str, target_url: str | None = None) -> None:
    """state: pending | success | failure | error. description: <=140 chars (GitHub truncates)."""
    if not (GITHUB_TOKEN and repo and sha):
        return
    body = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
    if target_url:
        body["target_url"] = target_url
    code, _ = _gh_api("POST", f"https://api.github.com/repos/{repo}/statuses/{sha}", body)
    log(f"commit status {state} for {repo}@{sha[:7]}: HTTP {code}")


# --------------------------------------------------------------------------- action runner


def _run(cmd: list[str], cwd: str | None, logfile: str, extra_env: dict | None = None) -> int:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    with open(logfile, "ab", buffering=0) as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n".encode())
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def _checkout(project: str, repo: str, sha: str, logfile: str) -> str:
    """Ensure repos/<project> is a clone of `repo`, fetched and checked out at `sha`. Returns path."""
    path = os.path.join(REPOS_DIR, project)
    clone_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{repo}.git"
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(REPOS_DIR, exist_ok=True)
        if _run(["git", "clone", clone_url, path], cwd=None, logfile=logfile) != 0:
            raise RuntimeError("git clone failed")
    # Always re-point origin (token rotates) and fetch the exact sha.
    _run(["git", "-C", path, "remote", "set-url", "origin", clone_url], cwd=None, logfile=logfile)
    if _run(["git", "-C", path, "fetch", "--depth", "50", "origin", sha], cwd=None, logfile=logfile) != 0:
        # Fall back to a full fetch if the shallow sha fetch isn't allowed.
        _run(["git", "-C", path, "fetch", "origin"], cwd=None, logfile=logfile)
    if _run(["git", "-C", path, "checkout", "-f", sha], cwd=None, logfile=logfile) != 0:
        raise RuntimeError(f"checkout {sha} failed")
    _run(["git", "-C", path, "clean", "-fdx"], cwd=None, logfile=logfile)
    return path


def do_check(job: dict) -> None:
    repo, sha, project = job["repo"], job["sha"], job["project"]
    logfile = os.path.join(LOG_DIR, f"check-{project}-{sha[:7]}.log")
    set_status(repo, sha, "pending", "platform-ci: running make check")
    try:
        _checkout(project, repo, sha, logfile)
        rc = _run([PLATFORM_BIN, "check", project], cwd=WORKSPACE, logfile=logfile)
    except Exception as e:  # noqa: BLE001
        log(f"check error {project}@{sha[:7]}: {e}")
        set_status(repo, sha, "error", f"platform-ci error: {e}")
        return
    set_status(repo, sha, "success" if rc == 0 else "failure",
               "checks passed" if rc == 0 else "make check failed")


def do_build_deploy(job: dict) -> None:
    repo, sha, project = job["repo"], job["sha"], job["project"]
    logfile = os.path.join(LOG_DIR, f"deploy-{project}-{sha[:7]}.log")
    set_status(repo, sha, "pending", "platform-ci: build + deploy")
    try:
        _checkout(project, repo, sha, logfile)
        cmd = [PLATFORM_BIN, "build", project, "--no-login"]
        if project in DEPLOY_ON_PUSH:
            cmd += ["--rollout", "--yes"]
        rc = _run(cmd, cwd=WORKSPACE, logfile=logfile)
    except Exception as e:  # noqa: BLE001
        log(f"build/deploy error {project}@{sha[:7]}: {e}")
        set_status(repo, sha, "error", f"platform-ci error: {e}")
        return
    if rc == 0:
        verb = "deployed" if project in DEPLOY_ON_PUSH else "image built"
        set_status(repo, sha, "success", f"platform-ci: {verb}")
    else:
        set_status(repo, sha, "failure", "build/deploy failed (see worker log)")


def worker_loop() -> None:
    while True:
        job = _jobs.get()
        try:
            log(f"running {job['action']} for {job['repo']}@{job['sha'][:7]}")
            (do_check if job["action"] == "check" else do_build_deploy)(job)
        except Exception as e:  # noqa: BLE001
            log(f"worker crash on job {job}: {e}")
        finally:
            _jobs.task_done()


# --------------------------------------------------------------------------- HTTP handler


def _verify_sig(secret: str, body: bytes, header: str) -> bool:
    if not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _enqueue(action: str, repo: str, sha: str, project: str) -> bool:
    try:
        _jobs.put_nowait({"action": action, "repo": repo, "sha": sha, "project": project})
        return True
    except queue.Full:
        log(f"queue full; dropped {action} for {repo}@{sha[:7]}")
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: ANN001 — silence default stderr spam; we log explicitly
        return

    def _reply(self, code: int, msg: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        if self.path != "/webhook":
            self._reply(404, "not found")
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not WEBHOOK_SECRET or not _verify_sig(WEBHOOK_SECRET, body, sig):
            log("rejected delivery: bad/missing HMAC signature")
            self._reply(401, "bad signature")
            return
        event = self.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._reply(400, "bad json")
            return

        if event == "ping":
            self._reply(200, "pong")
            return

        repo = (payload.get("repository") or {}).get("full_name", "")
        mapping = REPO_MAP.get(repo)
        if not mapping:
            self._reply(202, f"ignored: {repo} not registered")
            return
        project = mapping["project"]

        if event == "pull_request":
            if payload.get("action") not in {"opened", "synchronize", "reopened"}:
                self._reply(202, "ignored pr action")
                return
            sha = (((payload.get("pull_request") or {}).get("head")) or {}).get("sha", "")
            if sha and _enqueue("check", repo, sha, project):
                self._reply(202, "queued check")
            else:
                self._reply(202, "skipped")
            return

        if event == "push":
            default_branch = (payload.get("repository") or {}).get("default_branch", "main")
            if payload.get("ref") != f"refs/heads/{default_branch}":
                self._reply(202, "ignored non-default branch")
                return
            if payload.get("deleted"):
                self._reply(202, "ignored branch delete")
                return
            sha = payload.get("after", "")
            if sha and sha != "0" * 40 and _enqueue("build_deploy", repo, sha, project):
                self._reply(202, "queued build+deploy")
            else:
                self._reply(202, "skipped")
            return

        self._reply(202, f"ignored event {event}")


def main() -> int:
    if not WEBHOOK_SECRET:
        log("FATAL: PLATFORM_CI_WEBHOOK_SECRET not set")
        return 1
    os.makedirs(LOG_DIR, exist_ok=True)
    threading.Thread(target=worker_loop, daemon=True).start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log(f"platform-ci listening on {LISTEN_HOST}:{LISTEN_PORT}; repos={list(REPO_MAP)}; "
        f"deploy_on_push={sorted(DEPLOY_ON_PUSH)}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
