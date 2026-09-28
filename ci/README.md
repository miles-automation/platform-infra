# platform-ci

The GitHub-Actions-free CI/CD system from `docs/cicd-redesign.md` (D1–D7, §12). GitHub webhooks
drive `bin/platform` actions, which report GitHub commit statuses. There are no GitHub Actions
and no `.github/workflows`.

There are two modes. **On-demand** is the target. **Push** is the original always-on box, and it
keeps working until the cutover in `docs/platform-ci-ondemand-cutover.md` is done.

## On-demand mode (task 816)

```
GitHub ──webhook (HMAC)──► Caddy(ci.sparkswarm.com, platform droplet) ──► dispatcher.py
                                                                           │  SQLite queue
                                  DigitalOcean API ◄── create/destroy ─────┤  (volume)
                                        │                                  │
                                        ▼                                  │
                       CI box (droplet from platform-ci-snap-*)            │
                       runner.py ── claim / heartbeat / complete ─────────►┘
                           │ (HTTPS, Bearer PLATFORM_CI_RUNNER_TOKEN)
                           └─► worker.do_check / do_build_deploy ─► bin/platform ─► commit status
```

With no work queued, no CI droplet exists. DigitalOcean bills powered-off droplets, so the
dispatcher destroys idle boxes instead of powering them off.

### Dispatcher (`dispatcher.py`, platform droplet)

The dispatcher is a stdlib service. It runs as the `platform-ci-dispatcher` compose service
(`python:3.13-slim`, with `./ci` mounted read-only and SQLite on the `platform_ci_dispatcher`
volume). Caddy serves `ci.sparkswarm.com` and exposes only these paths: `/webhook`, `/healthz`,
`/status`, `/runner/*` and `/admin/*`.

- **Webhook:** HMAC verification is the same as `worker.py` (`X-Hub-Signature-256`, sha256,
  constant-time compare). Supported events are `pull_request` opened/synchronize/reopened (queues
  `check`), `pull_request` closed (drops the queued check) and a push to the default branch (queues
  `build_deploy`). The dispatcher records every `X-GitHub-Delivery` id, so a duplicate delivery
  is a no-op. An action/repo/SHA that is already queued or running is not queued a second time.
- **Queue rules:**
  - When a newer head arrives for the same PR, the queued check is marked `superseded`. Its
    commit status is `success`, with a description like `superseded: PR #12 head moved to
    abc1234; not run`, and never `failure`.
  - Before a check is handed out, the dispatcher asks GitHub for the PR's current head. A check
    whose PR has closed or has a different head is also superseded. If that lookup fails, the
    check runs anyway.
  - Queued `build_deploy` jobs coalesce per project to the newest push. The older ones are
    superseded in the same way. A deploy that is already running is never superseded.
  - Only one `build_deploy` runs at a time across the whole fleet, and only one box exists.
- **Statuses:** every queued job gets `pending: queued (N in queue)`. Status posts go through a
  persistent outbox, which retries in order up to 5 times, so a GitHub outage does not lose them.
- **Timeouts, so no status stays pending forever:**

| Condition | Result |
|---|---|
| Queued longer than `PLATFORM_CI_QUEUE_TIMEOUT_MINUTES` (60) | `error: not started within 60 min (no CI box)` |
| Running longer than `PLATFORM_CI_JOB_TIMEOUT_MINUTES` (90) | `error: timed out after 90 min`. The runner's next heartbeat is refused, so it kills the job and posts nothing more. |
| No heartbeat for `PLATFORM_CI_LEASE_TIMEOUT_SECONDS` (180), or the box stops polling, or the droplet disappears | The job is requeued once (`pending: requeued after …`). The second loss gives `error: CI box lost mid-job 2x`. A requeued deploy yields to a newer queued deploy for the same project. |
| Box create fails (API error, no snapshot) or the box never contacts the dispatcher within `PLATFORM_CI_BOOT_TIMEOUT_MINUTES` (10) | Retried after `PLATFORM_CI_CREATE_RETRY_SECONDS` (60). After `PLATFORM_CI_CREATE_MAX_FAILURES` (2), every queued job gets `error: could not start CI box: <reason>`. |
| Dispatcher down | GitHub marks the delivery failed. Redeliver it from the repo's webhook settings once the dispatcher is back. When the dispatcher restarts, it gives running leases and the box a fresh heartbeat window, so a restart never requeues a healthy job. |

### Box lifecycle

The reconcile loop runs every 15 s:

1. Expire leases and timeouts.
2. List droplets tagged `platform-ci-ondemand`. Only droplets whose name starts with
   `platform-ci-ondemand-` are considered, and all others are ignored. If the list call fails,
   nothing is created or destroyed.
3. If there is no known box and a tagged droplet exists, adopt the newest one and destroy any
   extras. This handles a dispatcher restart or a lost DB row, and means a droplet is never
   leaked or duplicated.
4. If there is work (queued, running or a `hold`) and no droplet exists, create one from the
   newest private image named `platform-ci-snap-*`. The defaults are `s-4vcpu-8gb` in `nyc3`
   with tag `platform-ci-ondemand` and the ssh key ids in `PLATFORM_CI_SSH_KEYS`. A create is
   attempted only when the tagged list is empty.
5. If no work has been seen for `PLATFORM_CI_IDLE_MINUTES` (10), destroy the box.

The dispatcher gives work only to the runner whose droplet id matches the box it created or
adopted. Any other runner gets a `403` and waits. That includes the old always-on box, if its
runner unit ever starts.

### Runner (`runner.py` + `platform-ci-runner.service`, CI box)

On boot, the unit fast-forwards `/srv/platform-ci/workspace/repos/platform-infra` to
`origin/main`. It then polls `POST /runner/claim` every 5 s, using its droplet id from the
metadata service. A claimed job runs through the existing `worker.do_check` /
`worker.do_build_deploy` code: the same checkout, node deps, per-check database, disk guard,
`bin/platform check|build --rollout`, post-deploy hook and commit statuses. The runner sends a
heartbeat every 30 s and reports completion. If completion cannot be delivered, it keeps
retrying and heartbeating. If the dispatcher says the lease is gone, the runner kills the job's
process group and suppresses any further statuses from it.

Check statuses name the configured target: for example, `platform-ci: running make ci` and
`make ci failed`.

The box needs these values in `/etc/platform-ci/env`, in addition to the push-mode values below:

```sh
PLATFORM_CI_DISPATCHER_URL=https://ci.sparkswarm.com
PLATFORM_CI_RUNNER_TOKEN=<same value as the dispatcher>
```

`PLATFORM_CI_MODE=runner bash ci/provision.sh` enables the runner unit and disables `platform-ci`
and `caddy`, effective from the next boot. This is what the snapshot should contain.

### Dispatcher configuration (`/root/platform-infra/.env`, sourced from Spark Swarm `spark-swarm/production`)

| Var | Purpose |
|---|---|
| `PLATFORM_CI_WEBHOOK_SECRET` | GitHub webhook secret (same value as today) |
| `PLATFORM_CI_RUNNER_TOKEN` | Bearer token for `/runner/*` (also baked into the snapshot) |
| `PLATFORM_CI_ADMIN_TOKEN` | Bearer token for `/status` and `/admin/hold` |
| `PLATFORM_CI_DO_TOKEN` | Scoped DO token (see the runbook for scopes); never logged |
| `PLATFORM_CI_GH_TOKEN` | `milesautomation-claude` PAT for commit statuses and PR head lookups |
| `PLATFORM_CI_REPO_MAP` | Same JSON as on the box, for example `{"miles-automation/slopticus":{"project":"slopticus"}}` |
| `PLATFORM_CI_SSH_KEYS` | Comma-separated DO ssh key ids added at create. The compose default is `46869945` ("nucleus-development-rich", the key in the CI box's root `authorized_keys`). |
| `PLATFORM_CI_BOX_SIZE` / `_REGION` / `_IDLE_MINUTES` | `s-4vcpu-8gb` / `nyc3` / `10` |

`PLATFORM_CI_DEPLOY_ON_PUSH` stays on the box. The dispatcher does not need it.

### Operator commands

```sh
python3.13 ./bin/platform ci status            # box + queue + recent jobs (PLATFORM_CI_ADMIN_TOKEN, else Spark Swarm)
python3.13 ./bin/platform ci hold 60           # keep a box up for 60 min (creates one if needed); `hold 0` releases
python3.13 ./bin/platform ci snapshot --dry-run
python3.13 ./bin/platform ci snapshot --yes    # live snapshot of the one tagged box, keep newest 2
python3.13 ./bin/platform ci snapshot --droplet-id 551995541 --yes   # first snapshot, from the old always-on box
```

`ci snapshot` runs only from the owner's Mac. It authenticates with the local doctl login
(`doctl auth token`), or `DIGITALOCEAN_ACCESS_TOKEN` if that is set, and never prints the token.
The dispatcher's scoped token cannot take or delete snapshots. The command waits for the snapshot action to complete. It prunes older
`platform-ci-snap-*` snapshots only after the new one exists.

To refresh the snapshot, for example after toolchain upgrades or to warm the Docker cache:

1. Run `ci hold 60` and wait for `ci status` to show the box as `ready`.
2. SSH in. Upgrade packages, pull caches, and edit `/srv/platform-ci/workspace/platform.toml` if
   needed. Leave `/etc/platform-ci/env` intact.
3. Run `ci snapshot --yes`, then `ci hold 0`.

The next box boots from the new image.

### Cold start and cost

- Cold start = DO create from snapshot, plus boot, plus the runner's git fetch, plus the first
  claim. The estimate is 1.5 to 3 minutes, and it will be recorded at cutover. Builds reuse the
  Docker layer cache and the per-repo clones baked into the snapshot. Refresh the snapshot to keep
  them warm.
- `s-4vcpu-8gb` costs $0.0714/h ($48/mo if always on, which is what the current box costs).
  An on-demand box is billed for its lifetime, including the 10-minute idle tail. DO bills
  Droplets per second; confirm this on the first invoice. For
  example, 2 h/day of box life comes to about $4.30/mo. Each snapshot costs $0.06/GB-month;
  about 19 GB used today gives about $1.15/mo per kept snapshot.

## Push mode (legacy always-on box, until cutover)

The box is `do-1gb-runner-main-…` (`167.172.224.151`, nyc3, currently `s-4vcpu-8gb` with a
25 GB disk). DNS `ci.sparkswarm.com` points to this box. Its own Caddy (`ci/Caddyfile`) proxies
`/webhook` and `/healthz` to `worker.py` on `127.0.0.1:8765`.

- `pull_request` (opened/synchronize/reopened) runs `check`: clone at the PR head SHA, run
  `bin/platform check <project>`, then post a commit status. `bin/platform check` runs the
  project's `check_target` from platform.toml (default `check`).
- A push to the default branch runs `build` (plus `deploy` if the repo is in `DEPLOY_ON_PUSH`):
  `bin/platform build <project> --rollout --yes`.

Deliveries are HMAC-verified and serialized: one job at a time, FIFO, with no superseding.

### Setup (one-time, per box)

These hold secrets and live ONLY on the box (never in git):

1. `/srv/platform-ci/workspace/platform.toml`, scp'd from the workspace root. The CI copy has
   drifted from the local one: edit it in place and never overwrite it.
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
4. An SSH key on the box that is authorized on the prod droplet (`bin/platform prod rollout`
   SSHes there).
5. `bash ci/provision.sh`, which installs deps and services and starts the worker.
6. Register the GitHub webhook: URL `https://ci.sparkswarm.com/webhook`, content type
   `application/json`, secret = `PLATFORM_CI_WEBHOOK_SECRET`, events: pushes and pull requests.

## Shared job behavior (both modes)

### Post-deploy hook

After a successful rollout, the worker runs the repo's own `deploy/post-deploy.sh`, if that file
exists and is executable. It runs from the checkout at the deployed SHA, with `REPO`, `SHA`,
`PROJECT` and the worker's `GH_TOKEN` in the environment. A non-zero exit fails the job with a
`post-deploy check failed` status.

### Disk guard

This has two layers:

- the fleet-wide `docker-prune.timer`, installed by `provision.sh`
- a pre-job check: if free space is below `PLATFORM_CI_MIN_FREE_GB` (default 5), the worker runs
  `/usr/local/bin/docker-prune` once. If space is still low, the job fails with a `disk low: …`
  status.

### Check-run database isolation

If the repo's compose file defines a `postgres` service, the worker starts it and keeps the
server. Each check gets its own throwaway `check_<sha>` database, which is dropped
`WITH (FORCE)` afterwards. Caveat: dev compose files map host port 5432, so a second registered
repo with its own postgres service would collide.

## Files

- `dispatcher.py`: on-demand dispatcher (platform droplet, compose service `platform-ci-dispatcher`).
- `runner.py`, `platform-ci-runner.service`: job puller on on-demand boxes.
- `worker.py`, `platform-ci.service`, `Caddyfile`: push-mode worker and its TLS proxy. The job
  functions in `worker.py` are shared with the runner.
- `provision.sh`: idempotent box setup (`PLATFORM_CI_MODE=runner` for snapshot images).
- Tests: `tests/test_ci_dispatcher.py`, `tests/test_ci_runner.py`, `tests/test_platform_ci_commands.py`.

## Ops

- Push mode: `/srv/platform-ci/logs/worker.log`, `systemctl restart platform-ci`.
- Runner: `/srv/platform-ci/logs/runner.log` (per-job logs as before), `systemctl status platform-ci-runner`.
- Dispatcher: `./bin/platform prod logs platform-ci-dispatcher --tail 200`, `python3.13 ./bin/platform ci status`.
- To add a repo, extend `PLATFORM_CI_REPO_MAP` on the dispatcher and in the box's env in the
  snapshot, add it to `PLATFORM_CI_DEPLOY_ON_PUSH` if it should auto-deploy, and register its
  webhook.

## Not done yet (tracked)

C5 Spark Swarm run integration; C6 `bin/platform onboard`; C7 scheduled SLO/drift; C8 fleet
migration + deleting every repo's `.github/workflows`.
