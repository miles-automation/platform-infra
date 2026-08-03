# Platform Infrastructure

Shared infrastructure for SparkSwarm projects. Manages Docker Compose services, Caddy reverse proxy, and Postgres database on a single DigitalOcean droplet.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Platform Droplet                              │
│                                                                       │
│  ┌─────────┐    ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  │
│  │  Caddy  │───▶│  IEOMD  │  │ Noodle  │  │  Umami  │  │ Postgres│  │
│  │  :80    │    │  :80    │  │  :8000  │  │  :3000  │  │  :5432  │  │
│  │  :443   │───▶│         │  │         │  │         │  │         │  │
│  └─────────┘    └─────────┘  └─────────┘  └─────────┘  └─────────┘  │
│       │                                        │              │       │
│       │              internal network          └──────────────┘       │
└───────┼──────────────────────────────────────────────────────────────┘
        │
   internet
```

## Services

| Service | Domain | Description |
|---------|--------|-------------|
| Caddy | - | Reverse proxy with automatic HTTPS |
| Postgres | - | Shared database (internal only) |
| IEOMD | ieomd.com | Time-locked secret delivery ([repo](https://github.com/richmiles/in-the-event-of-my-death)) |
| For Whenever | forwhenever.com | Private messages and files for later ([repo](https://github.com/richmiles/for-whenever)) |
| Noodle | callofthenoodle.com | Bar rating app |
| Umami | analytics.sparkswarm.com | Privacy-focused analytics |
| Spark Swarm | sparkswarm.com | Project dashboard + secrets manager |
| Human Index | humanindex.io | Private recall utility (subjects + observations) |
| Esher's Codex | esherscodex.com | Math curriculum app |
| richmiles.xyz | richmiles.xyz | Portfolio site + public API |
| Bullshit or Fit | bullshitorfit.com | Resume screening landing page + lead capture |
| Synapse | chat.sparkswarm.com | Matrix server (ops alerting) - planned |
| Uptime prober | - | Fleet health checks + Matrix alerts (Spark Swarm worker) |
| Email monitor | - | IMAP polling + Matrix notifications (Spark Swarm worker) |
| Lead scheduler | - | Lead outreach reminders + Matrix notifications (Spark Swarm worker) |

## Shared Resources

### Postgres
Each service gets its own database and user (defined in `init-db.sql`). Services connect via `DATABASE_URL` environment variable.

### Email
Mailbox hosting stays on MXRoute for inboxes, aliases, and catch-alls. Transactional sending and
Spark Swarm inbound webhook handling standardize on Postmark.

Operational rules:
- Store provider credentials in Spark Swarm secrets, not in repo-tracked files.
- Inject app-specific Postmark runtime secrets through `.env` only when a container needs them at
  startup, such as `HUMAN_INDEX_POSTMARK_API_TOKEN`.
- Treat Mailgun settings as legacy fallback only during the migration window.

### DigitalOcean Spaces (Object Storage)
Shared S3-compatible bucket (`platform-storage`) for file storage. Services use a prefix to isolate their objects:

| Service | Prefix | Example Key |
|---------|--------|-------------|
| IEOMD | `ieomd/` | `ieomd/attachments/{uuid}` |
| For Whenever | `for-whenever/` | `for-whenever/attachments/{uuid}` |

Configure in `.env`:
- `SPACES_BUCKET` - Bucket name (default: `platform-storage`)
- `SPACES_ACCESS_KEY` - DigitalOcean Spaces access key
- `SPACES_SECRET_KEY` - DigitalOcean Spaces secret key

## Quick Start

### 1. Clone to server

```bash
git clone git@github.com:richmiles/platform-infra.git
cd platform-infra
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

### 3. Start services

```bash
docker compose up -d
```

### 4. View logs

```bash
docker compose logs -f
```

## Platform CLI

`bin/platform` provides workspace and Spark Swarm operations. Lead pipeline commands use the
`SPARK_SWARM_API_KEY` environment variable:

```bash
python3.13 bin/platform leads list
python3.13 bin/platform leads list --status confirmed --overdue --limit 25
python3.13 bin/platform leads show <id-or-uuid>
python3.13 bin/platform leads advance <id-or-uuid> outreach_drafted
python3.13 bin/platform leads prospect --limit 15
python3.13 bin/platform leads prospect --min-score 60 --json
python3.13 bin/platform leads prospect --csv /tmp/hn-prospects.csv
```

The default lead project is `richmiles-xyz`; pass `--project` to select another configured Spark
or slug. `leads advance` follows the status transitions enforced by Spark Swarm.

`leads prospect` is read-only reconnaissance. It SSHes to the production droplet and queries the
`jobtrends.hn_hiring_posts` table inside the Bullshit or Fit container, selecting recent HN hiring
posts that mention both contract/freelance work and delivery-pipeline pain. It extracts a company,
contact, links, and a pipeline-role hint; scores and deduplicates by company; and prints a ranked
table with a footer. Use `--json` for machine output or `--csv PATH` to save the returned rows.
The query uses the observed `source=hn` and `stream=hiring` values; `stream=wants_hired` is excluded,
and a small raw-text filter removes clearly job-seeking posts that are mislabeled as hiring posts.
It does not send email, create leads, or write to production data.

## Initial Server Setup

For a fresh droplet, run the setup script:

```bash
curl -fsSL https://raw.githubusercontent.com/richmiles/platform-infra/main/setup.sh | bash
```

Or manually:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Install fail2ban
sudo apt update && sudo apt install -y fail2ban

# Configure firewall
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

## Adding a New Service

See `docs/adding-a-service.md` for a checklist, copy/paste templates (Compose + Caddy + env + DB), and deploy/rollback guidance.

### Service onboarding (tight version)

- Add container(s) to `docker-compose.yml` on the `internal` network (avoid publishing ports on the host).
- Add hostname routing to `Caddyfile` (edge Caddy only; point it at the internal service name + port).
- Add a dedicated Postgres DB + user (least privilege) and record required env vars in `.env.example`.
- Decide whether you need Spaces; if yes, use a per-service prefix (e.g. `myapp`) and keep credentials global (`SPACES_*`).
- Make migrations a one-off deploy step (run once), then restart services; keep schema changes backwards-compatible for rollbacks.
- Prefer pinned image tags (`sha-…`) for production rollouts so rollback is reverting a tag and restarting.

### Validate config

Docker Compose requires some env vars (passwords/secrets). A quick local validation is:

```bash
POSTGRES_PASSWORD=x \
IEOMD_DB_PASSWORD=x \
UMAMI_DB_PASSWORD=x \
UMAMI_APP_SECRET=x \
SYNAPSE_DB_PASSWORD=x \
HUMAN_INDEX_DB_PASSWORD=x \
HUMAN_INDEX_POSTMARK_API_TOKEN=x \
HUMAN_INDEX_SPARK_SWARM_OAUTH_CLIENT_SECRET=x \
ESHERS_CODEX_DB_PASSWORD=x \
SPARK_SWARM_DB_PASSWORD=x \
SPARK_SWARM_MASTER_KEY=x \
SPARK_SWARM_API_KEY=x \
UPTIME_PROBER_API_KEY=x \
EMAIL_MONITOR_API_KEY=x \
docker compose config >/dev/null
```

## Database Access

Connect to Postgres:

```bash
docker compose exec postgres psql -U postgres
```

Create a new database for a service:

```sql
CREATE USER myapp WITH PASSWORD 'secure-password';
CREATE DATABASE myapp_db OWNER myapp;
GRANT ALL PRIVILEGES ON DATABASE myapp_db TO myapp;
```

## Useful Commands

```bash
# Restart a single service
docker compose restart ieomd

# View service logs
docker compose logs -f caddy

# Pull latest images and restart
docker compose pull && docker compose up -d

# Check service status
docker compose ps

# Reload Caddy config (no downtime)
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Host Metrics Collector

The droplet can push host + container metrics to Spark Swarm every 5 minutes.

Files:
- `scripts/metrics-collector.py`
- `systemd/metrics-collector.service`
- `systemd/metrics-collector.timer`

Install on droplet:

```bash
cd /root/platform-infra
install -m 644 systemd/metrics-collector.service /etc/systemd/system/metrics-collector.service
install -m 644 systemd/metrics-collector.timer /etc/systemd/system/metrics-collector.timer
systemctl daemon-reload
systemctl enable --now metrics-collector.timer
```

Verify:

```bash
systemctl status metrics-collector.timer --no-pager
systemctl start metrics-collector.service
journalctl -u metrics-collector.service -n 50 --no-pager
```

## Backups

Automated PostgreSQL backup + Spaces upload is documented in `docs/backup.md`.

Install timers on the droplet:

```bash
cd /root/platform-infra
install -m 644 systemd/db-backup.service /etc/systemd/system/db-backup.service
install -m 644 systemd/db-backup.timer /etc/systemd/system/db-backup.timer
install -m 644 systemd/db-backup-health.service /etc/systemd/system/db-backup-health.service
install -m 644 systemd/db-backup-health.timer /etc/systemd/system/db-backup-health.timer
systemctl daemon-reload
systemctl enable --now db-backup.timer db-backup-health.timer
```

Run a manual backup:

```bash
cd /root/platform-infra
scripts/db_backup.sh run
```

## Docker Disk Guard

Both droplets have filled their root disks with docker debris and broken deploys with cryptic
ENOSPC errors (prod 2026-07-20, platform-ci 2026-08-02). `scripts/docker_prune.sh` runs daily
via `systemd/docker-prune.timer` on **both** the prod droplet and the platform-ci droplet: it
prunes old exited containers, unused images older than 7 days (running containers' images and
recent rollback tags are kept), and build cache older than 3 days, then escalates to a full
unused prune if the disk is still ≥80% full. It never touches volumes. Thresholds are
env-overridable (`DOCKER_PRUNE_THRESHOLD_PCT`, `DOCKER_PRUNE_IMAGE_UNTIL`,
`DOCKER_PRUNE_BUILDER_UNTIL`).

The CI worker also fails jobs fast with a clear `disk low` commit status (after trying one
prune) instead of letting npm/docker die mid-build — see `ci/README.md`.

Install on a droplet (the ci box gets this automatically from `ci/provision.sh`):

```bash
cd /root/platform-infra
install -m 0755 scripts/docker_prune.sh /usr/local/bin/docker-prune
install -m 644 systemd/docker-prune.service /etc/systemd/system/docker-prune.service
install -m 644 systemd/docker-prune.timer /etc/systemd/system/docker-prune.timer
systemctl daemon-reload
systemctl enable --now docker-prune.timer
```

Verify / run once by hand:

```bash
systemctl status docker-prune.timer --no-pager
systemctl start docker-prune.service
journalctl -u docker-prune.service -n 50 --no-pager
```

## Directory Structure

```
platform-infra/
├── docker-compose.yml       # Service definitions
├── Caddyfile                # Reverse proxy config
├── .env.example             # Environment template
├── .env                     # Local environment (git-ignored)
├── init-db.sql              # Postgres initialization
├── setup.sh                 # Server bootstrap script
├── AGENTS.md                # Safe deployment practices
├── WHEN_SOMETHING_BREAKS.md # Incident response runbook
├── README.md                # This file
├── scripts/
│   ├── db_backup.sh         # Postgres backup + Spaces upload + retention
│   ├── docker_prune.sh      # Docker disk guard (images/build-cache/containers)
│   └── metrics-collector.py # Host metrics ingest
├── systemd/
│   ├── db-backup.service
│   ├── db-backup.timer
│   ├── db-backup-health.service
│   ├── db-backup-health.timer
│   ├── docker-prune.service
│   ├── docker-prune.timer
│   ├── metrics-collector.service
│   └── metrics-collector.timer
└── docs/
    ├── backup.md            # Backup + restore runbook
    ├── adding-a-service.md  # Service onboarding guide
    └── SPARKSWARM_BRAND.md  # SparkSwarm infrastructure overview
```
