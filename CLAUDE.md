# CLAUDE.md - platform-infra (Production)

This repo is the source of truth for the shared production droplet (`159.65.241.127`) and its Docker Compose stack.

## Safety

- Assume every change is production-impacting.
- Do not restart/stop `postgres`, delete volumes, or run `docker compose down` unless explicitly approved.

## How deploys work (fleet standard)

- Each app service uses `image: ghcr.io/miles-automation/<app>:{${APP_IMAGE_TAG}:-latest}` (tag is pinned in `/root/platform-infra/.env`).
- App repos promote changes via `./bin/platform prod rollout <project> --tag sha-<short> --yes`, which:
  - Sets `<APP>_IMAGE_TAG=sha-...` in `/root/platform-infra/.env`
  - `docker compose pull <service>`
  - Runs migrations (if configured)
  - `docker compose up -d <service>`
  - Health-checks `https://<domain>/healthz`

## Recommended operator commands (workspace helper)

Run from the workspace root:

- Sync infra to droplet: `./bin/platform prod sync infra --yes`
- Restart a service: `./bin/platform prod deploy <service> --yes`
- Tail logs: `./bin/platform prod logs <service> --tail 200`
- Restart caddy (dangerous): `./bin/platform prod caddy restart --yes --really`

## Files that matter

- `docker-compose.yml` - running services and image tags
- `Caddyfile` - domain routing
- `.env.example` - documented env vars (actual `.env` lives only on the droplet)
- `init-db.sql` - initial Postgres DB/user creation (only applies on fresh Postgres)
