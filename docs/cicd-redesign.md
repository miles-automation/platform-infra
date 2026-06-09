# CI/CD Redesign — Design Doc (DRAFT for review)

> **Status:** proposal, awaiting Rich's annotations. Nothing built yet. This is the
> "decide what we want and how we run it" doc before any code moves. Annotate the
> **Open Decisions** inline — each is a real fork.

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

## 7. Open Decisions (annotate inline)

### D1 — How do the pipelines run? `[FORK]`
- **(a) Platform-native** — a small `platform-ci` worker we own: webhook → Spark Swarm run →
  `bin/platform` stages → GitHub commit status. Most consistent with everything else; CI/CD
  becomes a first-class part of the platform; all our code. **More to build/maintain.**
- **(b) Off-the-shelf (Woodpecker CI)** — install Woodpecker on the CI droplet; pipelines are
  small config files that invoke `bin/platform` stages. Mature UI/retries, less custom code;
  **a third-party component not integrated with Spark Swarm.**
- **(c) Hybrid** — Woodpecker triggers/runs; `bin/platform` does the work; Spark Swarm records
  the event. Splits the difference; two systems to understand.
- _Rich:_ <!-- pick a/b/c + why -->

### D2 — CI droplet sizing & lifecycle `[FORK]`
Fresh droplet (the 1 GB one is dead). 2 GB (builds fit, tight) vs 4 GB (headroom for parallel
builds + a CI server). Dedicated CI box vs fold onto an existing one (not the prod box).
- _Rich:_ <!-- size + dedicated? -->

### D3 — Is Spark Swarm the CI/CD control plane? `[FORK]`
Spark Swarm already has runs/events/secrets/SLO. Making CI/CD runs *be* Spark Swarm runs is
the most "platform-native" story (one dashboard, retryable, audited). Counter: it couples CI to
Spark Swarm's availability. Yes / partial (events only) / no.
- _Rich:_ <!-- -->

### D4 — Trigger mechanism `[FORK]`
GitHub **webhook** → CI droplet (real-time, needs a public endpoint + HMAC verify) vs **poll**
GitHub for new commits (simpler, no inbound endpoint, slight latency) vs **git push-to-deploy**
to a droplet remote (no GitHub dependency at all for triggering).
- _Rich:_ <!-- -->

### D5 — Keep ephemeral staging droplets? `[FORK]`
The current `/stage` flow spins up a throwaway DO droplet per PR (`ephemeral_stage.py`). Keep
as-is, simplify to a shared staging service, or drop staging for now (deploy straight to prod
with rollback)?
- _Rich:_ <!-- -->

### D6 — Registry: keep ghcr.io? `[FORK]`
Images live in GitHub Container Registry today. "Off GitHub infra" could extend to a
self-hosted registry on the droplet. Keep ghcr (free, works, low effort) vs self-host
(full independence, more to run). _Recommendation: keep ghcr for now — it's not the cost or
the pain point._
- _Rich:_ <!-- -->

### D7 — What's the merge gate without GitHub Actions? `[FORK]`
Today PR "required checks" are GHA jobs. Off GHA, the gate becomes a **commit status** posted
by the platform-ci worker (GitHub still enforces "status must be green to merge"), plus
commit-guard locally. Confirm that's the model (vs. trusting commit-guard alone on unprotected
repos).
- _Rich:_ <!-- -->

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
