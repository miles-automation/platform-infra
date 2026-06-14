# CI/CD Redesign — Design Doc

> **Status:** **decisions made (D1–D7, with Rich, 2026-06-08); implementation plan written
> (§12).** Build the `platform-ci` worker per the C-queue, pilot on human-index-v2. The big
> deploy footgun surfaced during the v2 manual deploy (§11) is already fixed.

## 1. Why we're doing this

The fleet's CI/CD is **legacy and should be rethought from the ground up** (Rich's call):

- **~70 copy-pasted GitHub Actions YAML files** — 7 workflow types (`ci`, `build`,
  `ephemeral-staging`, `promote-production`, `generate-token`, `reliability-slo`,
  `docs-drift`) duplicated across ~11 repos. Each repo re-implements the same orchestration.
- **Tied to GitHub's infrastructure.** We want **off GitHub Actions entirely** — GitHub
  stays only as a git host + image registry (ghcr.io). The goal is independence, consistency,
  and a forward-looking system, **not** cost (cost is not the driver right now).
- **The one self-hosted runner is dead.** `do-1gb-runner-main` (`167.172.224.151`) has been
  OOM-killed since 2026-03-19 — a 1 GB droplet cannot build container images (they need
  ~2–4 GB). It is also an *org-level GitHub Actions runner*, so reviving it would keep us
  **on** GitHub Actions. We are not reviving it.

**The key realization:** the legacy system's problem is not its logic — it's that the logic
lives in per-repo GitHub YAML. **We already own every primitive a CI/CD needs.**

## 2. What we already own (the substrate)

| Capability | Where it lives today | Notes |
|---|---|---|
| Orchestration CLI | `bin/platform` (`repos/platform-infra/bin/platform`, symlinked at workspace root) | `check`, `prod rollout` (health-check + **auto-rollback**), `prod deploy/sync/secrets`, `secrets`, `dns`, `spark run`, `infra`. ~2500 lines, already the deploy backbone. |
| Run / event engine | **Spark Swarm** (`sparkswarm.com/api/v1`) | Already models **runs** (`/runs`, start/status/resume), **events**, **secrets**, and an **SLO gate** (`/runs/slo/gate`). Effectively a workflow engine we built. |
| Local pre-push gate | **commit-guard** (`.claude/skills/commit-guard/guard.py` + `.githooks/pre-push`) | The GHA-free CI model **human-index-v2 already uses**: runs `make check` + static checks (secrets, conflict markers, migration hygiene, lockfile drift). Blocks the push before code leaves the dev box. |
| Declarative per-repo contract | `platform.toml` (workspace) + `deploy/pack.toml` (per repo) | `platform.toml`: image name, infra service, domains, secrets project, DNS. `pack.toml`: services, ports, routes, DB migrations, **health checks**, staging droplet spec, required secrets. |
| Deploy substrate | Prod droplet `159.65.241.127` + `/root/platform-infra/.env` (`*_IMAGE_TAG` pins) + ghcr images | `bin/platform prod rollout <project> --tag sha-…` already does: set tag → pull → migrate (`pack.toml`) → restart → health-check via Caddy → **rollback on failure** → log a Spark Swarm event. |

The GHA workflows are thin wrappers around scripts we own — e.g. `promote-production.yml`
calls `spark-swarm/runner/prod_deploy.py`, which is ≈ `bin/platform prod rollout`.

## 3. Current action inventory (what the legacy system actually does)

| Workflow | Trigger | What it does | Owns logic? |
|---|---|---|---|
| `ci.yml` | PR, push main, dispatch | `make check` (lint/format/type/test) + contract/docs-drift | → `make check` (ours) |
| `build.yml` | dispatch | docker build → ghcr push + Trivy scan | thin (docker + ghcr login) |
| `ephemeral-staging.yml` | dispatch, `/stage` PR comment | provision DO droplet → deploy via `pack.toml` → report to PR → destroy | → `runner/ephemeral_stage.py` (ours) |
| `promote-production.yml` | dispatch | build+push → SSH prod → set tag → pull/migrate/up → health check | → `runner/prod_deploy.py` ≈ `bin/platform prod rollout` |
| `generate-token.yml` | dispatch | POST a capability-token mint to the prod API | thin (a curl) — only 2 repos |
| `reliability-slo.yml` | dispatch, daily cron | GET Spark Swarm SLO gate, fail if not passing | thin (a curl) — only spark-swarm |
| `docs-drift.yml` | PR, push main | validate Caddyfile / compose / AGENTS.md / CLAUDE.md consistency | a contract check |

**Takeaway:** the load-bearing logic (`make check`, `prod rollout`, `ephemeral_stage.py`,
`prod_deploy.py`, the SLO gate, secrets) is **already ours and already droplet-native**. GHA
is just the trigger + glue.

## 4. Principles for the fresh design

1. **Define the pipeline once.** No per-repo pipeline code. Repos stay purely declarative
   (`platform.toml` + `pack.toml`); the *how* lives in one place.
2. **Droplet-native, GitHub-free orchestration.** GitHub = git host + ghcr registry only. No
   GitHub Actions, no `.github/workflows`.
3. **Leverage what we own.** Build on `bin/platform` + Spark Swarm + commit-guard, don't
   re-introduce a foreign orchestration layer unless it clearly earns its keep.
4. **Observable + auditable.** Every CI/CD run is a first-class, retryable record (Spark Swarm
   run) and reports a **GitHub commit status** so PRs still show green/red.
5. **Fast local feedback.** commit-guard stays the pre-push gate — most failures caught before
   anything hits the droplet.
6. **Consistent across the fleet.** One model every repo uses; the fleet contract
   (`platform.toml`/`pack.toml`) is the consistency seam.

## 5. The action set (rethought — fewer, unified)

| action | trigger | what runs |
|---|---|---|
| `check` | push / PR | `make check` + the contract/docs-drift check (folded into one) |
| `build` | `check` passes on the default branch | docker build → ghcr + vuln scan |
| `stage` | `/stage` on a PR (or manual) | ephemeral droplet deploy (reuse `ephemeral_stage.py`) |
| `deploy` | manual / tag (human-gated) | `bin/platform prod rollout` (rollback built in) |
| `scheduled` | cron | SLO gate + drift checks |

**Retire / relocate:** `generate-token` → a plain `bin/platform` subcommand (it's a one-shot
API call, not a pipeline). The per-repo YAML sprawl → deleted.

**Open question on staging** — see Decision D5.

## 6. Architecture (the shape; the engine is Decision D1)

```
 dev box ──(git push, blocked unless commit-guard passes)──► GitHub (git + ghcr)
                                                               │  webhook (push / PR / comment)
                                                               ▼
                                                   ┌─────────────────────────┐
                                                   │  CI droplet              │
                                                   │  platform-ci worker      │  ◄── Decision D1: how
                                                   │   • receive webhook      │       this is built
                                                   │   • run bin/platform <action>
                                                   │   • report status        │
                                                   └───────────┬─────────────┘
                                   GitHub commit status ◄──────┤
                                   Spark Swarm run/event  ◄─────┘
                                                               │  deploy actions
                                                               ▼
                                              prod droplet 159.65.241.127 (unchanged path)
```

- **Trigger:** GitHub webhook (push/PR/comment) → the CI droplet. (Alternative: poll. See D4.)
- **Worker:** runs the matching `bin/platform` action, streams a Spark Swarm run, and sets a
  GitHub commit status (so PR checks still gate merges) — **without** GitHub Actions.
- **Builds** need RAM → a **fresh, right-sized CI droplet** (2–4 GB), not the dead 1 GB box.
- **Deploy path is unchanged** — `bin/platform prod rollout` already does it well.

## 7. Decisions (RESOLVED 2026-06-08, with Rich)

| | Decision |
|---|---|
| **D1 — engine** | **Build our own thin `platform-ci` worker.** `bin/platform` is already the engine (the v2 deploy proved it); a webhook → `bin/platform` action → commit-status worker is small + fully ours. Woodpecker would mostly just call `bin/platform` anyway. |
| **D2 — CI droplet** | **Fresh, dedicated 2 GB droplet** (the 1 GB box OOM'd on builds). Replaces the dead runner. |
| **D3 — control plane** | **Yes — Spark Swarm.** Each CI run *is* a Spark Swarm run/event → observability + retries + audit without building a UI. |
| **D4 — trigger** | **GitHub webhook** (HMAC-verified) → the worker. |
| **D5 — staging** | **Drop ephemeral staging.** Deploy straight to prod with the health-check + auto-rollback (proven on v2), gated by `check` + commit-guard. Re-add staging only if a real need appears. |
| **D6 — registry** | **Keep ghcr.io** — works, free, not the pain point. Self-hosted `registry:2` already on the droplet as a fallback. |
| **D7 — merge gate** | **Commit-status gate.** The worker posts a GitHub commit status; "must be green to merge" still enforced, zero GHA. commit-guard stays the local pre-push gate. |

## 8. Migration plan (pilot-first)

1. **Decide** D1–D7 (this doc).
2. **Stand up** the CI droplet + the chosen engine; wire **one** GitHub webhook.
3. **Pilot on `human-index-v2`** — it's already GHA-free, so it's the clean test: `check` +
   `build` + `deploy` fully platform-native, commit-status gating PRs.
4. **Roll out fleet-wide**, repo by repo: move each onto the platform-native pipeline, then
   **delete its `.github/workflows`**. Update the fleet contract checker to *forbid*
   `.github/workflows` (drift check inverts: their presence becomes the violation).
5. **Decommission** the dead 1 GB runner droplet + remove org-runner registration.

## 9. Explicitly keep vs. retire

**Keep (reuse as-is):** `bin/platform` (extend with `check`/`build`/`stage` actions if needed),
`prod rollout` + rollback, `ephemeral_stage.py` / `prod_deploy.py` logic, `platform.toml` /
`pack.toml` contract, commit-guard, ghcr images, the prod-droplet deploy path, Spark Swarm
secrets/events.

**Retire:** all `.github/workflows/*.yml` (70+ files), the org GitHub Actions runner + its
1 GB droplet, the `runner_target`/`ubuntu-latest` dual-runner expressions, per-repo
`generate-token.yml`.

## 10. Risks / watch-items

- **Single CI droplet = single point of failure** for builds/deploys. Mitigate with a simple
  restart/health story (and the deploy path still works by hand via `bin/platform`).
- **Webhook endpoint** is new attack surface — HMAC-verify GitHub deliveries; least-privilege.
- **Secrets**: the worker needs ghcr push creds, prod SSH key, Spark Swarm API key — same
  secrets GHA holds today, now on a box we control (arguably better). Keep them in Spark Swarm
  secrets + droplet `.env`, not in the repo.
- **commit-guard is the only gate on `git push`**, but unprotected branches mean a determined
  push can skip it — the commit-status gate on PRs is the real enforcement for merges.

## 11. Lessons from the v2 manual deploy (2026-06-08)

We hand-deployed human-index-v2 to `humanindex.io` (apex cutover; v1 → dormant) end-to-end to
surface what the automated system must handle. What we hit:

1. **🔴 Single-file bind mount + rsync = silently stale config (the big one). ✅ FIXED
   (main `cd4f8d0`).** `bin/platform prod sync infra` rsyncs `Caddyfile`, which **replaces the
   file's inode**. Caddy's `./Caddyfile:/etc/caddy/Caddyfile:ro` mount was pinned to the *old*
   inode, so `caddy reload` re-read the **stale** file — the route change silently didn't apply
   (container saw v1 while the host file said v2). **Every Caddyfile change via the documented
   sync+reload path silently no-op'd.** **Fix applied:** moved the Caddyfile to `caddy/Caddyfile`
   and mount the **directory** (`./caddy:/etc/caddy:ro`), which reflects current contents
   regardless of how the file is replaced. One-time `docker compose up -d caddy` recreate;
   afterwards sync+reload propagates with no restart (verified live). The pipeline's `deploy`
   action should still treat config propagation as first-class — this bug class recurs with any
   single-file config mount.

2. **🔴 Health-check parity can't confirm *which* app is serving.** v1 and v2 both answer
   `/healthz` → `{"status":"ok","db":"ok"}`, so the cutover *looked* done (200 OK) while still
   serving v1. Only the OpenAPI route set revealed it. **Deploy verification must check an
   identity/version signal** (a `/version` endpoint, the image SHA, or a known route), not just
   health 200s.

3. **🟡 New-service onboarding is a large manual surface.** A working app still needed ~9
   artifacts/registrations across 3 repos + the droplet: `deploy/pack.toml`, `/healthz` +
   `/api/v1/healthz`, a compose service, a Caddy route, a DB + role, droplet `.env` vars, a
   Spark Swarm secret, a `platform.toml` entry, (DNS already existed). **The platform needs a
   `bin/platform onboard <project>` that scaffolds all of these from `pack.toml`/`platform.toml`.**

4. **🟡 Health-endpoint convention drift.** v2 shipped only `/health`; the fleet contract +
   `prod rollout` want DB-checked `/healthz` + `/api/v1/healthz`. The pipeline `check` action (or
   a scaffold lint) should enforce the contract endpoints.

5. **🟡 Secrets onboarding gap.** The global Spark Swarm API key is **scoped to one project**, so
   it couldn't write a *new* project's secret (`403 insufficient_scope`). New-project onboarding
   must provision a per-project secrets key — the worker/onboard flow needs an admin-scoped key.

6. **🟡 "New service takes an existing domain" isn't a `prod rollout` flow.** `prod rollout`
   health-checks *via the domain*, which still points at the old service until cutover. A
   takeover needs **bring-up → verify internally → cutover route → verify externally**, a
   distinct sequence the pipeline should model (vs. same-service tag bumps).

7. **🟢 Boot window ~30s** (alembic + `uv` startup). The deploy health-check must allow for it
   (the rollout's 45×2s window does). Codify a generous default.

8. **🟢 Already present: a self-hosted Docker registry** (`registry:2` on the droplet,
   `127.0.0.1:5000`, "GHCR fallback / GitHub-free deploys"). D6 (registry independence) is
   *already half-built* — worth folding into the design rather than defaulting to ghcr.

**Net:** the deploy *substrate* (`bin/platform prod rollout`, the image→ghcr→pull path, the
contract files) is solid. The gaps are **onboarding scaffolding**, **config-propagation
correctness** (the bind-mount bug), and **identity-aware verification** — exactly what the
pipeline `check`/`build`/`deploy` actions plus an `onboard` command should own.

## 12. Implementation plan (`platform-ci`)

The decided system: a small **`platform-ci` worker** we own, on a dedicated 2 GB droplet, that
receives a GitHub webhook → runs the matching `bin/platform` action → posts a GitHub commit
status + records a Spark Swarm run. commit-guard stays the local pre-push gate; ghcr stays the
registry; no ephemeral staging; deploy is prod-with-rollback. GitHub = git host + registry only.

**Worker shape:** a FastAPI service. `POST /webhook` (HMAC-verified) → map `(repo, event)` to
`(project from platform.toml, action)` → run the action → report. Concurrency serialized (small
box). Holds: a GitHub token (commit status + clone), ghcr push creds, the prod SSH key, a Spark
Swarm key — in the CI droplet `.env` / Spark Swarm, never the repo.

Ordered queue (each ~one focused session; pilot every action on **human-index-v2** first — it's
already GHA-free):

- **C1 — CI droplet + worker skeleton + reachable webhook.** Provision a fresh 2 GB DO droplet
  (Docker + Caddy for TLS). Stand up the worker with an HMAC-verified `/webhook` that logs
  deliveries. Route `ci.sparkswarm.com` (or similar) → worker. Register one GitHub webhook
  (org-level). **Done:** a push delivers a verified webhook to the worker.
- **C2 — `check` action (first real pipeline).** On push/PR for a registered project: clone at
  the SHA, run `make check` (in a container to avoid toolchain drift), post a GitHub
  **commit status** (pending → success/failure). **Done:** a v2 PR is gated by a platform-ci
  status, no GHA. *(This is the MVP — C1+C2 replace `ci.yml` for one repo.)*
- **C3 — `build` action.** On merge to the default branch (or a tag): `docker build` →
  push ghcr `sha-<short>`, record the image. **Done:** merging v2 main yields a pushed image.
  - **Local build command shipped (2026-06-14):** `bin/platform build <project> [--tag] [--rollout]`
    resolves `ghcr_image`/`repo_dir` from `platform.toml`, builds `linux/amd64` on the
    **docker-driver builder for the active docker context** (NOT a docker-container/QEMU builder —
    that hangs indefinitely on this stack's cross-build, observed ~80min/zero-output), verifies the
    image arch, pushes to ghcr, and with `--rollout` chains into `prod rollout`. This is the
    worker's `build` step, runnable by hand today; the webhook trigger (C1–C2) still needs the CI
    droplet. Until then, deploy = `bin/platform build <project> --rollout --yes`.
- **C4 — `deploy` action.** Human-gated trigger (a `/deploy` comment, a tag, or `bin/platform ci
  deploy`) → worker runs `bin/platform prod rollout <project> --tag sha-<short>` (health-check +
  rollback already built in). **Done:** a v2 deploy runs end-to-end through the worker.
- **C5 — Spark Swarm run integration.** Each action opens/updates a Spark Swarm run + emits
  start/step/result events. **Done:** CI runs are visible + retryable in the Spark Swarm dashboard.
- **C6 — `bin/platform onboard <project>`.** Scaffold a new fleet service from the v2 lessons:
  `deploy/pack.toml`, `/healthz` + `/api/v1/healthz` check, compose service stanza, Caddy route,
  DB + role, a secret, a `platform.toml` entry. **Done:** onboarding a service is one command,
  not ~9 manual steps.
- **C7 — Scheduled checks.** Cron (or Spark Swarm scheduled runs): SLO gate + uptime + fleet
  drift. Replaces `reliability-slo.yml` and the scheduled `docs-drift`.
- **C8 — Fleet migration + GHA removal.** Per repo: register the webhook, confirm
  check/build/deploy, then **delete `.github/workflows`**. Flip the fleet-contract checker to
  **forbid** `.github/workflows` (their presence becomes the violation). Decommission the dead
  1 GB runner droplet + the org GitHub Actions runner registration.

**Milestones:** C1–C2 = one repo gated off GHA (the proof). C3–C4 = full v2 lifecycle on
`platform-ci`. C6 = new services are cheap. C8 = fleet off GitHub Actions entirely.
