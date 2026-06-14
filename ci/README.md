# platform-ci

The GitHub-Actions-free CI/CD trigger from `docs/cicd-redesign.md` (D1–D7, §12). A tiny stdlib
webhook worker on a dedicated droplet that runs `bin/platform` actions and reports GitHub commit
statuses — no GitHub Actions, no `.github/workflows`.

## Box

`do-1gb-runner-main` resized to **s-1vcpu-2gb** (`167.172.224.151`, nyc3). It is itself
`linux/amd64`, so app images build **natively** — no QEMU cross-build (the hang that broke the
manual v2 deploy). DNS `ci.sparkswarm.com` → this droplet; Caddy terminates TLS and proxies
`/webhook` + `/healthz` to the worker on `127.0.0.1:8765`.

## Flow

```
GitHub ──webhook (HMAC sha256)──► Caddy(ci.sparkswarm.com) ──► worker.py
   ▲                                                              │
   └────────────── commit status ◄──────────  bin/platform <action>
```

- `pull_request` (opened/synchronize/reopened) → `check`: clone at PR head SHA → `bin/platform
  check <project>` → commit status. This is the PR merge gate (replaces `ci.yml`).
- `push` to the default branch → `build` (+ `deploy` if the repo is in `DEPLOY_ON_PUSH`):
  `bin/platform build <project> --rollout --yes` (native build → ghcr → prod rollout w/ rollback).

Deliveries are HMAC-verified and serialized (one job at a time; small box).

## Files

- `worker.py` — the service (stdlib only; no FastAPI/uvicorn to maintain).
- `platform-ci.service` — systemd unit (`EnvironmentFile=/etc/platform-ci/env`).
- `Caddyfile` — TLS reverse proxy.
- `provision.sh` — idempotent box setup (deps + caddy + uv + checkout + services).

## Setup (one-time, per box)

These hold secrets and live ONLY on the box (never in git):

1. `/srv/platform-ci/workspace/platform.toml` — scp'd from the workspace root.
2. `/etc/platform-ci/env` (chmod 600):
   ```sh
   PLATFORM_CI_WORKSPACE=/srv/platform-ci/workspace
   PLATFORM_CI_WEBHOOK_SECRET=<random 32+ bytes; same value in the GitHub webhook>
   GH_TOKEN=<milesautomation-claude PAT: repo + write:packages>
   SPARK_SWARM_API_KEY=<for prod rollout event logging>
   PLATFORM_CI_REPO_MAP={"miles-automation/human-index-v2":{"project":"human-index-v2"}}
   PLATFORM_CI_DEPLOY_ON_PUSH=human-index-v2
   ```
3. `docker login ghcr.io` (so `bin/platform build --no-login` can push).
4. An SSH key on the box authorized on the prod droplet (`bin/platform prod rollout` SSHes there).
5. `bash ci/provision.sh` — installs deps, services, starts the worker.
6. Register the GitHub webhook → `https://ci.sparkswarm.com/webhook`, content-type `application/json`,
   secret = `PLATFORM_CI_WEBHOOK_SECRET`, events: pushes + pull requests.

## Ops

- Logs: `/srv/platform-ci/logs/worker.log`; per-job logs `deploy-<proj>-<sha>.log` / `check-…`.
- Restart: `systemctl restart platform-ci`. Status: `systemctl status platform-ci`.
- Add a repo: extend `PLATFORM_CI_REPO_MAP` (+ `PLATFORM_CI_DEPLOY_ON_PUSH` to auto-deploy it),
  restart the worker, register its webhook.

## Not done yet (tracked)

C5 Spark Swarm run integration; C6 `bin/platform onboard`; C7 scheduled SLO/drift; C8 fleet
migration + deleting every repo's `.github/workflows`.
