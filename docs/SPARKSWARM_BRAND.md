# SparkSwarm Infrastructure

SparkSwarm is the shared infrastructure namespace for Rich Miles' projects. It provides common services that projects can use without maintaining their own instances.

## Services

| Domain | Service | Purpose |
|--------|---------|---------|
| analytics.sparkswarm.com | Umami | Privacy-focused analytics |
| chat.sparkswarm.com | Matrix/Synapse | Operational alerting |
| ci.sparkswarm.com | platform-ci | GitHub-Actions-free CI/CD webhook worker |

## What Projects Get

Projects hosted on SparkSwarm infrastructure automatically inherit:

**Analytics**
- Page views and basic metrics via Umami
- No cookies, no personal data collection
- Self-hosted (no third-party analytics)

**Alerting**
- Error notifications via Matrix
- Operational alerts for rate limits, scheduler failures
- Single ops room for all projects

## Projects Using SparkSwarm

| Project | Domain | Description |
|---------|--------|-------------|
| IEOMD | ieomd.com | Time-locked secret delivery |
| Noodle | callofthenoodle.com | Bar rating app |

## What SparkSwarm Is Not

- **Not a company** - just an infrastructure namespace
- **Not customer-facing** - no public branding or marketing
- **Not a product** - internal tooling only

Projects don't need to mention SparkSwarm to users. The infrastructure is invisible to end users.

## Adding a New Project

1. Add service to `docker-compose.yml`
2. Add domain routing to `Caddyfile`
3. Configure analytics: add Umami script with new website ID
4. Configure alerting: add Matrix room/token to project config

## Infrastructure Details

All services run on a single DigitalOcean droplet with:
- Caddy for reverse proxy and automatic HTTPS
- PostgreSQL for shared database (isolated per-service)
- DigitalOcean Spaces for object storage (optional)

See [README.md](../README.md) for operational details.
