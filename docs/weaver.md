# Weaver deployment

Service `weaver` serves https://weaver.sparkswarm.com on internal port 8787. It uses the `weaver_data` volume at `/app/data` and has no published ports or Postgres dependency. The process runs as the image's non-root `node` user with a read-only root filesystem.

Source: `miles-automation/code-loom`, app PR #18, release commit `c456b94572caa72b261226da3c01537cc71fa24b`. Initial tag: `sha-c456b94`.

Secrets are stored in Spark Swarm project `code-loom`, environment `production`: `WEAVER_OWNER_TOKEN`, `WEAVER_VAPID_PUBLIC_KEY`, `WEAVER_VAPID_PRIVATE_KEY`, and `WEAVER_VAPID_SUBJECT`. Use `platform prod secrets apply code-loom production --yes`; never rotate the VAPID pair independently.

The workspace project config must set `secret_export_allowlist` to those four keys plus `CODE_LOOM_IMAGE_TAG`. The Secrets API export includes global fallbacks; filtering prevents them from shadowing shared production credentials. Use the updated platform helper with this allowlist for subsequent refreshes.

The initial release uses `platform prod deploy-tarball` because the automation GitHub token lacks GHCR package-write scope. Images retain their versioned GHCR names locally on the droplet; do not use a pull-based rollout until the tag is published to the registry. Caddy changes use validated graceful reloads.

Health: `/health` returns 200; `/api/inbox` must return 401 without login. Check `docker compose ps weaver` for container health. Owner login and session keys must not be printed in logs. Physical-phone push receipt still requires a subscribed device and smoke test.

First-release rollback: remove the Weaver Caddy block and reload Caddy, then stop only `weaver`. Keep `weaver_data` and the secrets for recovery. Do not stop shared services or delete the volume.
