# Verified application delivery

`platform prod release-status <project>` is read-only. It reports the configured service's running image reference, Docker image ID, full OCI revision label, running/health state and desired image tag as JSON. Container environment and unrelated secrets are not returned. Missing containers or conflicting deployment state require operator inspection.

`platform prod rollout <project> --tag sha-<revision> --previous-verified-tag sha-<previous> --yes` uses the existing backup, migration, health and rollback flow. The optional previous tag must come from an actually observed healthy release. It prevents rollback to an unverified desired tag when desired configuration differs from the running image. The command does not independently establish that evidence; the managed delivery driver records it before invoking rollout.

Non-dry-run rollouts acquire an exclusive lock per configured service under `runtime/rollout-locks` on this workspace host. Competing rollouts for the same service fail immediately; other services may proceed. The operating system releases the lock after process exit or failure. The lock file is retained for future calls. This is local serialization, not a distributed production lock: operators using another host must coordinate releases.

The Weaver delivery driver binds each build to the reviewed merged revision and a delivery UUID, records the previous release, checks the actual image and authenticated product behavior, and refuses to replay an uncertain rollout. Both `/healthz` and `/api/v1/healthz` are required for Spark Swarm. Shared services and PostgreSQL are not restarted by this feature.
