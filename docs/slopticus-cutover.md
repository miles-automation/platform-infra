# Slopticus hard cutover — task 721

Companion application PR: https://github.com/miles-automation/code-loom/pull/121.

This replaces the Weaver service and route with Slopticus, using `slopticus.com`, `www.slopticus.com`, the `ghcr.io/miles-automation/slopticus` image, `SLOPTICUS_OWNER_TOKEN`, `SLOPTICUS_IMAGE_TAG` and a fresh `slopticus_data` volume. Encrypted phone relay is enabled. No old database is copied or deleted.

## Coordinated delivery

1. Obtain production secret access for the existing `code-loom` project in Spark Swarm. The current agent key returns HTTP 403: scoped to `spark-swarm`, not `code-loom`. Store a fresh owner token through Spark Swarm; do not hand-edit the droplet environment.
2. Update the deployment project's image and secret allowlist to match this configuration. Build and publish the reviewed app revision as the new image before switching the service.
3. Apply the workspace configuration changes below, export/apply the new secrets with the platform CLI, and inspect production drift before syncing only the reviewed infrastructure changes.
4. Start only `slopticus`; verify `/healthz` reports the reviewed version and commit. Reload Caddy and verify HTTPS at the apex and the www redirect. Stop only the old `weaver` service after the replacement is verified; do not remove its volume.
5. Publish the signed Slopticus desktop downloads and new iPhone app. Install fresh, enroll Macs and pair phones again. Verify phone replies and approvals on a physical device.
6. Retire the old release runner after checking that no release is running; provision the new runner and its signed universal helper. Preserve old local application data and signing credentials.

DNS apex A and www CNAME were applied and resolved to `159.65.241.127` and `slopticus.com` respectively on 2026-09-25. DNS alone does not establish HTTPS or a live application.

## Workspace registry changes at cutover

Keep the internal `projects.code-loom` key, repository path, Spark UUID and worktree prefix, since shared work and review history use them. Change these product/deployment fields in workspace `platform.toml`:

```toml
display_name = "Slopticus"
domains = ["slopticus.com", "www.slopticus.com"]
ghcr_image = "ghcr.io/miles-automation/slopticus"
infra_service = "slopticus"
required_secrets = ["SLOPTICUS_OWNER_TOKEN"]
secret_export_allowlist = ["SLOPTICUS_OWNER_TOKEN", "SLOPTICUS_IMAGE_TAG"]

[projects.code-loom.dns]
provider = "cloudflare"
cloudflare_token_secret = "CLOUDFLARE_API_TOKEN"
cloudflare_token_project = "spark-swarm"
cloudflare_token_environment = "production"
cloudflare_proxied = false
```

Do not merge or deploy this service configuration before its required secrets and image exist. Task 721 remains in progress until coordinated review, delivery verification and cleanup are complete.
