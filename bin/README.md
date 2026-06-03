# Platform workspace CLI

`platform` is the workspace CLI for the `platform/` multi-repo workspace —
project discovery, worktrees, local/prod ops, secrets, and DNS. It is the tool
behind the `./bin/platform ...` invocations referenced throughout `CLAUDE.md`
and `AGENTS.md`.

`platform-infra` is its canonical version-controlled home. The CLI is run from
the **workspace root** (one level above `repos/`), where it reads `platform.toml`
and operates on the sibling `repos/*` checkouts — it is not executed on the
droplet. The workspace's `./bin/platform` should point at (or be kept in sync
with) the copy tracked here.

## Scripts

- `platform` — the CLI (Python 3.11+, stdlib only; uses `tomllib`).
- `claude-platform` / `codex-platform` — thin launchers that start the
  respective agent via `platform agent <name>` with the right GitHub identity
  and Spark Swarm actor key.

## Notes

- Secrets are resolved at runtime from Spark Swarm; none are hardcoded here.
- Production health checks probe **HTTPS** via `curl --resolve <domain>:443:127.0.0.1`
  rather than HTTP, because bare-domain Caddy sites auto-redirect HTTP→HTTPS with
  a 308 that `curl -f` treats as success — an HTTP probe would pass even with the
  backend down, defeating the rollout's auto-rollback.
