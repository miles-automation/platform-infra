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

The backup holds `/run/lock/platform-file-backup.lock` for the whole run (dumps, then volumes),
and the photo cleanup timer takes the same lock, so a database dump and the volume archive from
one run always agree. Hold it for restores too.

Restore Esher's Codex (database and photos together, from the same run). Hold the lock for the
whole restore so a backup cannot dump restored rows while the photos are half extracted:

```bash
cd /root/platform-infra
eval "$(python3 - ./.env <<'PY'
import pathlib, shlex, sys
for raw in pathlib.Path(sys.argv[1]).read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if key.isidentifier():
        print(f"export {key}={shlex.quote(value)}")
PY
)"
stamp=<YYYY-MM-DD_HHMMSS>
db=eshers_codex_db_${stamp}.sql.gz
photos=platform-infra_eshers_codex_photos_${stamp}.tar.gz
# Skip the download if /root/backups still has the local copies (kept 7 days).
for f in "$db" "$photos"; do
  docker run --rm -v /root/backups:/root/backups \
    -e AWS_ACCESS_KEY_ID="$SPACES_ACCESS_KEY" -e AWS_SECRET_ACCESS_KEY="$SPACES_SECRET_KEY" \
    -e AWS_DEFAULT_REGION="${SPACES_REGION:-nyc3}" amazon/aws-cli \
    --endpoint-url "${SPACES_ENDPOINT:-https://${SPACES_REGION:-nyc3}.digitaloceanspaces.com}" \
    s3 cp "s3://$SPACES_BUCKET/${DB_BACKUP_SPACES_PREFIX:-postgres}/$f" /root/backups/
done
gunzip -t "/root/backups/$db" && tar -tzf "/root/backups/$photos" >/dev/null \
  && gunzip -c "/root/backups/$db" | tail -n 5 | grep -q "PostgreSQL database dump complete" \
  || { echo "backup files are damaged or incomplete; nothing was changed"; exit 1; }
flock -w 3600 /run/lock/platform-file-backup.lock bash -euo pipefail -c '
  pg() { docker run --rm -i -e PGPASSWORD="$1" postgres:18-alpine psql -v ON_ERROR_STOP=1 \
    -h "${DB_BACKUP_DB_HOST:-10.108.0.5}" -U "$2" -d "$3"; }
  docker compose stop eshers-codex
  echo "DROP DATABASE IF EXISTS eshers_codex_db WITH (FORCE);
        CREATE DATABASE eshers_codex_db OWNER ${ESHERS_CODEX_DB_USER:-eshers_codex};" \
    | pg "$POSTGRES_PASSWORD" "${POSTGRES_USER:-postgres}" postgres
  gunzip -c "/root/backups/$1" \
    | pg "$ESHERS_CODEX_DB_PASSWORD" "${ESHERS_CODEX_DB_USER:-eshers_codex}" eshers_codex_db
  docker run --rm -v platform-infra_eshers_codex_photos:/data -v /root/backups:/backups:ro \
    alpine:3 sh -euc "find /data -mindepth 1 -delete && tar -xzf /backups/$2 -C /data"
  docker compose up -d eshers-codex
' bash "$db" "$photos"
```

`.env` is not valid shell, so it is loaded the same way `scripts/db_backup.sh` loads it. Both files
are checked before anything is stopped or dropped (gzip and tar integrity, and the
dump's closing `PostgreSQL database dump complete` line). Inside the lock any failed step stops
the restore with the app left stopped, so it never starts against a half-restored database.
Drill 2026-10-31: this procedure, pointed at a scratch database and volume, restored a test photo's
row and a byte-identical file from the 22:14 UTC run downloaded from Spaces.
The dumps are taken with `--no-owner`, so the database is restored as the app user into a fresh
database it owns. To restore photos only, run just the `alpine` step inside the same `flock`.

Photos are kept 90 days by `systemd/eshers-codex-photo-cleanup.timer`, which runs
`app.chores_cli cleanup-photos` inside the container daily:

```bash
install -m 644 systemd/eshers-codex-photo-cleanup.service /etc/systemd/system/
install -m 644 systemd/eshers-codex-photo-cleanup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eshers-codex-photo-cleanup.timer
```
