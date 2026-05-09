# AGENTS.md - Platform Infrastructure

This repo manages shared infrastructure for all SparkSwarm projects on a single DigitalOcean droplet.

## Safety First

**This repo controls production infrastructure. Changes can cause downtime.**

### DO NOT without explicit user approval:
- Restart or stop `postgres` (data loss risk)
- Modify `init-db.sql` on a running system (won't apply without recreation)
- Delete Docker volumes
- Run `docker compose down` (stops all services)
- Modify `.env` on the droplet directly

### Safe operations (can do when asked):
- Add new services to `docker-compose.yml`
- Add new domains to `Caddyfile`
- Restart individual app services (not postgres)
- Pull and update images
- View logs

## Current Services

| Service | Domain | Port | Notes |
|---------|--------|------|-------|
| caddy | - | 80, 443 | Reverse proxy, auto HTTPS |
| postgres | - | 5432 | Shared DB (internal only) |
| ieomd | ieomd.com | 8000 | IEOMD app (FastAPI serves built SPA + API) |
| umami | analytics.sparkswarm.com | 3000 | Privacy-focused analytics |
| spark-swarm | sparkswarm.com | 8000 | Project dashboard + secrets manager |
| synapse | chat.sparkswarm.com | 8008 | Matrix server (ops alerting) - planned |
| noodle | callofthenoodle.com | 8000 | Bar rating app |
| human-index | humanindex.io | 8000 | Private recall utility |
| eshers-codex | esherscodex.com | 8000 | Math curriculum app |
| richmiles-xyz | richmiles.xyz | 8000 | Static resume site |
| bullshit-or-fit | bullshitorfit.com | 8000 | Landing + lead capture |
| uptime-prober | - | - | Fleet uptime prober (Spark Swarm worker) |
| email-monitor | - | - | IMAP polling + Matrix notifications (Spark Swarm worker) |
| lead-scheduler | - | - | Lead outreach scheduler (Spark Swarm worker) |

## Secrets Management

**Use Spark Swarm for secrets** instead of manually editing `.env` files.

Email standard:
- MXRoute hosts inboxes, aliases, and catch-alls.
- Postmark is the default provider for transactional outbound email and Spark Swarm inbound webhook handling.
- Treat Mailgun configuration as a temporary fallback unless the user explicitly asks to keep or restore it.

```bash
# Store a secret
curl -X POST https://sparkswarm.com/api/v1/secrets \
  -H "X-API-Key: $SPARK_SWARM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "MYAPP_DB_PASSWORD", "value": "secret", "project": "myapp", "environment": "production"}'

# Get a secret
curl "https://sparkswarm.com/api/v1/secrets/resolve/MYAPP_DB_PASSWORD?project=myapp&environment=production" \
  -H "X-API-Key: $SPARK_SWARM_API_KEY"

# Export all secrets for a project as .env
curl "https://sparkswarm.com/api/v1/secrets/export/dotenv?project=myapp&environment=production" \
  -H "X-API-Key: $SPARK_SWARM_API_KEY"
```

Pre-stored secrets:
- `CLOUDFLARE_API_TOKEN` - For managing sparkswarm.com DNS
- `SPARK_SWARM_API_KEY` - For accessing the secrets manager itself

## Adding a New Service

### 1. Update `docker-compose.yml`

```yaml
  myapp:
    image: ghcr.io/miles-automation/myapp:${MYAPP_IMAGE_TAG:-latest}
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://${MYAPP_DB_USER:-myapp}:${MYAPP_DB_PASSWORD}@postgres:5432/${MYAPP_DB_NAME:-myapp_db}
      # OR for SQLite:
      # DATABASE_URL: sqlite:///./data/myapp.db
    volumes:
      - myapp_data:/app/data  # if using SQLite or local storage
    networks:
      - internal
    depends_on:
      postgres:
        condition: service_healthy  # only if using Postgres
```

Add to `volumes:` section if needed:
```yaml
volumes:
  myapp_data:
```

Add to caddy's `depends_on:`:
```yaml
  caddy:
    depends_on:
      - myapp
```

### 2. Update `Caddyfile`

```
myapp.example.com {
    reverse_proxy myapp:8000
}

www.myapp.example.com {
    redir https://myapp.example.com{uri} permanent
}
```

### 3. Update `.env.example`

```bash
# MyApp
MYAPP_DB_USER=myapp
MYAPP_DB_PASSWORD=change-me
MYAPP_DB_NAME=myapp_db
MYAPP_IMAGE_TAG=latest
```

### 4. Update `init-db.sql` (if using Postgres)

```sql
-- MyApp
CREATE USER myapp WITH PASSWORD 'change-me';
CREATE DATABASE myapp_db OWNER myapp;
GRANT ALL PRIVILEGES ON DATABASE myapp_db TO myapp;
```

Note: `init-db.sql` only runs on fresh Postgres containers. For existing deployments, run the SQL manually.

### 5. Deploy

```bash
# Sync to droplet
rsync -avz --exclude='.git' --exclude='.env' . root@159.65.241.127:/root/platform-infra/

# SSH to droplet
ssh root@159.65.241.127

# Pull and start new service
cd /root/platform-infra
docker compose pull myapp
docker compose up -d myapp

# Restart Caddy to pick up new domain
docker compose restart caddy
```

## Secrets (SparkSwarm) - Current Injection Model

SparkSwarm Secrets API is the source of truth for production secrets, but services still load env vars from `/root/platform-infra/.env` on the droplet (Docker Compose).

Workflow:
1) Store/update secrets in SparkSwarm:
```bash
curl -X POST https://sparkswarm.com/api/v1/secrets \
  -H "X-API-Key: $SPARK_SWARM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"HUMAN_INDEX_DB_PASSWORD","value":"generated-password","project":"human-index","environment":"production"}'
```

2) Export to dotenv (review first):
```bash
curl -s "https://sparkswarm.com/api/v1/secrets/export/dotenv?project=human-index&environment=production" \
  -H "X-API-Key: $SPARK_SWARM_API_KEY"
```

3) Apply on droplet and restart the service:
```bash
# Prefer using workspace helper (idempotent block replace):
./bin/platform prod secrets apply human-index production --yes
./bin/platform prod deploy human-index --yes
```

Note: the droplet needs `SPARK_SWARM_API_KEY` available to fetch exports (currently stored in `/root/platform-infra/.env`).

## Troubleshooting

### Service won't start
```bash
docker logs platform-infra-<service>-1
```

### Wrong architecture (exec format error)
The droplet is linux/amd64. Rebuild with:
```bash
docker build --platform linux/amd64 -t ghcr.io/miles-automation/<app>:latest .
```

### Can't pull from ghcr.io (unauthorized)
Either make the package public on GitHub, or auth on droplet:
```bash
# Get token locally
gh auth token

# Auth on droplet
echo '<token>' | docker login ghcr.io -u richmiles --password-stdin
```

### Caddy not serving new domain
1. Check DNS is pointing to droplet: `dig +short <domain>`
2. Restart Caddy: `docker compose restart caddy`
3. Check Caddy logs: `docker logs platform-infra-caddy-1`

### Database connection issues
```bash
# Check postgres is healthy
docker compose ps postgres

# Connect manually
docker compose exec postgres psql -U postgres
```

## File Structure

```
platform-infra/
├── docker-compose.yml       # Service definitions
├── Caddyfile                # Domain routing
├── .env.example             # Environment template
├── .env                     # Actual secrets (git-ignored)
├── init-db.sql              # Postgres init script
├── setup.sh                 # Fresh server bootstrap
├── README.md                # Overview and commands
├── AGENTS.md                # This file
├── WHEN_SOMETHING_BREAKS.md # Incident response runbook
└── docs/
    └── SPARKSWARM_BRAND.md  # SparkSwarm infrastructure overview
```

## Key Documentation

- **[WHEN_SOMETHING_BREAKS.md](WHEN_SOMETHING_BREAKS.md)** - Triage checklist and common fixes for production issues
- **[docs/SPARKSWARM_BRAND.md](docs/SPARKSWARM_BRAND.md)** - What SparkSwarm is and what services it provides
