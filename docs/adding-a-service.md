# Adding a new service

This platform is a single DigitalOcean droplet running Docker Compose with:

- **Caddy (edge)**: terminates TLS and routes by hostname
- **Postgres**: shared database (internal-only)
- **Per-project services**: usually a `web` container and (optionally) a `backend` container

The goal is that adding a project is mostly copy/paste plus a small set of decisions.

## Checklist (decisions)

Before editing files, decide:

- **Domain(s)**: `app.example.com`, redirects (`www` → apex), and whether it needs a temporary host for testing.
- **Traffic model**:
  - Static site + API: `web` serves frontend and proxies `/api/*` to `backend`.
  - API-only: edge Caddy routes directly to `backend`.
- **Ports**:
  - Internal port(s) exposed inside the `internal` Docker network (no host ports).
  - Only edge Caddy binds `:80/:443` on the host.
- **Health**:
  - A `/health` endpoint for backend services.
  - A quick manual check command you can run after deploy.
- **Database**:
  - Does it need Postgres? If yes, define a dedicated DB + user.
  - Migration strategy: migrations should be a one-off, serialized step during deploy.
- **Object storage (optional)**:
  - If using Spaces, pick a unique prefix (e.g. `myapp`) for isolation.
- **Secrets**:
  - What needs to be in `.env` (DB password, app secrets, API keys, etc.).

## Files you will touch

- `docker-compose.yml`: add your service(s)
- `Caddyfile`: add hostname routing
- `.env.example`: document new env vars
- `init-db.sql`: add DB/user for fresh installs (and/or apply manually on existing installs)

## Templates (copy/paste)

### 1) `docker-compose.yml` service skeleton

Pick a short service name (e.g. `myapp-web`, `myapp-backend`) and add it to `docker-compose.yml`:

```yaml
  myapp-web:
    image: ghcr.io/miles-automation/myapp-web:${MYAPP_IMAGE_TAG:-latest}
    restart: unless-stopped
    networks: [internal]
    # If the web container needs to know its external URL:
    # environment:
    #   SITE_ADDRESS: ":80"  # Internal HTTP only (edge Caddy handles TLS)
    depends_on:
      - myapp-backend

  myapp-backend:
    image: ghcr.io/miles-automation/myapp-backend:${MYAPP_IMAGE_TAG:-latest}
    restart: unless-stopped
    networks: [internal]
    environment:
      DATABASE_URL: postgresql://${MYAPP_DB_USER:-myapp}:${MYAPP_DB_PASSWORD:?Set MYAPP_DB_PASSWORD in .env}@postgres:5432/${MYAPP_DB_NAME:-myapp_db}
    depends_on:
      postgres:
        condition: service_healthy
```

Notes:

- Do not publish ports on the host (no `ports:`) unless there is a deliberate reason.
- If your backend depends on Postgres, use the `service_healthy` dependency like the existing services.
- Add your service to the `caddy.depends_on` list so edge routing comes up cleanly after upstreams.

### API-only service template

If your service is API-only, skip the `web` container and route edge Caddy to your backend directly.

Compose:

```yaml
  myapi:
    image: ghcr.io/miles-automation/myapi:${MYAPI_IMAGE_TAG:-latest}
    restart: unless-stopped
    networks: [internal]
    environment:
      DATABASE_URL: postgresql://${MYAPI_DB_USER:-myapi}:${MYAPI_DB_PASSWORD:?Set MYAPI_DB_PASSWORD in .env}@postgres:5432/${MYAPI_DB_NAME:-myapi_db}
    depends_on:
      postgres:
        condition: service_healthy
```

Caddy:

```caddyfile
api.example.com {
	reverse_proxy myapi:8000
}
```

### 2) `Caddyfile` hostname routing

Add a new block to route traffic to the internal service:

```caddyfile
myapp.example.com {
	reverse_proxy myapp-web:80
}
```

If you need redirects:

```caddyfile
www.myapp.example.com {
	redir https://myapp.example.com{uri} permanent
}
```

### 3) `.env.example` keys

Add a new section for your service:

```bash
# =============================================================================
# MyApp
# =============================================================================
MYAPP_DB_USER=myapp
MYAPP_DB_PASSWORD=change-me-myapp-password
MYAPP_DB_NAME=myapp_db
MYAPP_IMAGE_TAG=latest  # or a pinned tag like sha-xxxxxxx
```

If the service needs Spaces, ensure the platform-level `SPACES_*` variables are set, and add service-specific toggles/prefixes as needed. For services that follow the IEOMD pattern (S3-compatible settings), the backend container commonly takes:

```bash
MYAPP_OBJECT_STORAGE_ENABLED=false
MYAPP_OBJECT_STORAGE_PREFIX=myapp
```

### 4) `init-db.sql` (fresh installs only)

`init-db.sql` is only applied when the Postgres volume is created for the first time.
If Postgres is already running with an existing volume, apply the SQL manually (see below).

Add a new block:

```sql
-- Create MyApp database and user
CREATE USER myapp WITH PASSWORD 'CHANGE_ME_MYAPP';
CREATE DATABASE myapp_db OWNER myapp;
GRANT ALL PRIVILEGES ON DATABASE myapp_db TO myapp;
```

## Deploy + verify

On the droplet:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f --tail=200 myapp-backend myapp-web
```

If you need to apply DB/user changes on an existing Postgres volume:

```bash
cat <<'SQL' | docker compose exec -T postgres psql -U postgres
CREATE USER myapp WITH PASSWORD 'CHANGE_ME_MYAPP';
CREATE DATABASE myapp_db OWNER myapp;
GRANT ALL PRIVILEGES ON DATABASE myapp_db TO myapp;
SQL
```

Then verify at the edge:

```bash
curl -fsSL https://myapp.example.com/health
```

## Migrations (recommended model)

Treat migrations as a one-off step per deploy (run once), then roll the service containers.
Avoid “run migrations on container startup” for shared infrastructure.

Pattern (example; use the service’s actual command/tooling):

```bash
docker compose run --rm myapp-backend alembic upgrade head
docker compose up -d myapp-backend myapp-web
```

## Rollbacks

- Keep image tags pinned (`sha-…`) for production changes so rollback is just reverting a tag and restarting.
- Prefer backwards-compatible schema changes (expand/contract) so an older image can still run after a rollback.
