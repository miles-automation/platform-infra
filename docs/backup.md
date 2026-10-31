# PostgreSQL Backup Automation

This runbook covers automated Spark Swarm PostgreSQL backups on the production droplet.

## What It Does

- Runs `pg_dump` against `spark_swarm_db` from the `postgres` container.
- Compresses output to `/root/backups/<prefix>_YYYY-MM-DD_HHMMSS.sql.gz`.
- Uploads to DigitalOcean Spaces.
- Verifies upload via `head-object`.
- Applies retention:
  - local: keep 7 days
  - remote: keep all daily backups for 30 days
  - remote: keep Sunday snapshots for 90 days
- Runs a health check hourly and sends Matrix alerts if:
  - last upload failed
  - last backup is older than `DB_BACKUP_HEALTH_MAX_AGE_HOURS` (default 25h)

## Files

- Script: `scripts/db_backup.sh`
- Timers/services:
  - `systemd/db-backup.service`
  - `systemd/db-backup.timer`
  - `systemd/db-backup-health.service`
  - `systemd/db-backup-health.timer`

## Required Environment Variables

These live in `/root/platform-infra/.env` and are documented in `.env.example`.

- `SPACES_BUCKET`
- `SPACES_ACCESS_KEY`
- `SPACES_SECRET_KEY`
- `SPACES_REGION` (default `nyc3`)
- `SPACES_ENDPOINT` (default `https://nyc3.digitaloceanspaces.com`)
- `DB_BACKUP_LOCAL_DIR` (default `/root/backups`)
- `DB_BACKUP_PREFIX` (default `spark_swarm`)
- `DB_BACKUP_SPACES_PREFIX` (default `postgres`)
- `DB_BACKUP_AWS_CLI_IMAGE` (default `amazon/aws-cli:latest`)
- `DB_BACKUP_HEALTH_MAX_AGE_HOURS` (default `25`)

Optional Matrix alerting:

- `MATRIX_HOMESERVER_URL`
- `MATRIX_ACCESS_TOKEN`
- `MATRIX_ROOM_ID`

Note: the backup script runs AWS CLI via Docker (`amazon/aws-cli`), so host-level `awscli` install is not required.

## Install on Droplet

```bash
cd /root/platform-infra
install -m 644 systemd/db-backup.service /etc/systemd/system/db-backup.service
install -m 644 systemd/db-backup.timer /etc/systemd/system/db-backup.timer
install -m 644 systemd/db-backup-health.service /etc/systemd/system/db-backup-health.service
install -m 644 systemd/db-backup-health.timer /etc/systemd/system/db-backup-health.timer
systemctl daemon-reload
systemctl enable --now db-backup.timer db-backup-health.timer
```

## Manual Verification

```bash
cd /root/platform-infra
scripts/db_backup.sh run
scripts/db_backup.sh health-check
```

Check systemd status/logs:

```bash
systemctl status db-backup.timer --no-pager
systemctl status db-backup-health.timer --no-pager
journalctl -u db-backup.service -n 100 --no-pager
journalctl -u db-backup-health.service -n 100 --no-pager
```

## Restore Procedure

1. Pick backup object:

```bash
aws --endpoint-url "$SPACES_ENDPOINT" s3 ls "s3://$SPACES_BUCKET/$DB_BACKUP_SPACES_PREFIX/"
```

2. Download backup:

```bash
aws --endpoint-url "$SPACES_ENDPOINT" s3 cp \
  "s3://$SPACES_BUCKET/$DB_BACKUP_SPACES_PREFIX/<backup-file>.sql.gz" \
  "/tmp/<backup-file>.sql.gz"
```

3. Restore (destructive to current target DB):

```bash
gunzip -c "/tmp/<backup-file>.sql.gz" | docker compose exec -T postgres psql -U "${POSTGRES_USER:-postgres}"
```

4. Verify critical tables exist and row counts are sane:

```bash
docker compose exec -T postgres psql -U "${POSTGRES_USER:-postgres}" -d "${SPARK_SWARM_DB_NAME:-spark_swarm_db}" \
  -c "\\dt"
```

## Restore Test (Recommended)

At least once per quarter:

1. Run `scripts/db_backup.sh run`.
2. Create a disposable test table in `spark_swarm_db`.
3. Drop the table.
4. Restore latest backup.
5. Confirm pre-existing production tables/data are readable.

## File volumes

The same run also archives the Docker volumes listed in `DB_BACKUP_VOLUMES` (default
`platform-infra_eshers_codex_photos`, the Esher's Codex chore photos) as
`<volume>_YYYY-MM-DD_HHMMSS.tar.gz`, with the same Spaces prefix, verification and retention as
the dumps. A missing volume or failed upload counts as a failed backup and alerts.

Restore Esher's Codex photos (stop nothing else; the app tolerates a missing file with a 404):

```bash
cd /root/platform-infra
docker run --rm -v /root/backups:/backups amazon/aws-cli --endpoint-url "$SPACES_ENDPOINT" \
  s3 cp "s3://$SPACES_BUCKET/postgres/<archive>.tar.gz" /backups/   # or use the local copy
docker run --rm -v platform-infra_eshers_codex_photos:/data -v /root/backups:/backups alpine:3 \
  tar -xzf /backups/<archive>.tar.gz -C /data
```

Restore the matching `eshers_codex_db` dump first or alongside, so photo rows and files agree.
Photos are kept 90 days by `systemd/eshers-codex-photo-cleanup.timer`, which runs
`app.chores_cli cleanup-photos` inside the container daily:

```bash
install -m 644 systemd/eshers-codex-photo-cleanup.service /etc/systemd/system/
install -m 644 systemd/eshers-codex-photo-cleanup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eshers-codex-photo-cleanup.timer
```
