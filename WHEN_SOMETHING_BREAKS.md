# When Something Breaks

Quick reference for diagnosing and fixing issues on the platform droplet.

## Quick Reference

```
Droplet IP: 159.65.241.127
SSH:        ssh root@159.65.241.127
```

## Triage Checklist

Work through these in order. Most issues are one of the first three.

### 1. Is the site reachable at all?

```bash
curl -I https://ieomd.com
```

- **Connection refused** → Caddy is down or firewall issue
- **SSL error** → Caddy certificate issue
- **502/503** → Backend service is down
- **200 OK** → Site is up, problem is elsewhere

### 2. Are containers running?

```bash
ssh root@159.65.241.127 "docker ps --format 'table {{.Names}}\t{{.Status}}'"
```

Look for containers that are restarting or exited.

### 3. Check the logs

```bash
# Caddy (reverse proxy)
ssh root@159.65.241.127 "docker logs platform-infra-caddy-1 --tail 50"

# IEOMD frontend
ssh root@159.65.241.127 "docker logs platform-infra-ieomd-1 --tail 50"

# IEOMD backend
ssh root@159.65.241.127 "docker logs platform-infra-backend-1 --tail 50"

# Postgres
ssh root@159.65.241.127 "docker logs platform-infra-postgres-1 --tail 50"

# Umami
ssh root@159.65.241.127 "docker logs platform-infra-umami-1 --tail 50"
```

### 4. Is it DNS?

```bash
dig ieomd.com +short
dig analytics.sparkswarm.com +short
```

Should return `159.65.241.127`. If not, check DigitalOcean DNS settings.

## Common Issues

### Service not responding

Restart the specific service:

```bash
ssh root@159.65.241.127 "cd /root/platform-infra && docker compose restart ieomd"
ssh root@159.65.241.127 "cd /root/platform-infra && docker compose restart backend"
```

### Database connection errors

Check Postgres is healthy:

```bash
ssh root@159.65.241.127 "docker exec platform-infra-postgres-1 pg_isready"
```

If unhealthy, check logs and restart:

```bash
ssh root@159.65.241.127 "docker logs platform-infra-postgres-1 --tail 100"
ssh root@159.65.241.127 "cd /root/platform-infra && docker compose restart postgres"
```

**Warning:** Restarting Postgres affects all services. Only do this if necessary.

### SSL certificate issues

Caddy handles certificates automatically. If there's an issue:

```bash
ssh root@159.65.241.127 "cd /root/platform-infra && docker compose restart caddy"
```

If certificates are still failing, check Caddy logs for rate limit errors (Let's Encrypt has limits).

### Container keeps restarting

Check logs for the crash reason:

```bash
ssh root@159.65.241.127 "docker logs platform-infra-backend-1 --tail 200"
```

Common causes:
- Missing environment variable → check `.env` on droplet
- Database migration needed → run migrations
- Out of memory → check `docker stats`

### Disk space full

```bash
ssh root@159.65.241.127 "df -h"
```

If full, clean up Docker:

```bash
ssh root@159.65.241.127 "docker system prune -f"
```

## Rollback Procedure

If a deployment broke something:

1. Identify the last working image tag
2. Update `.env` on droplet with previous tag:
   ```bash
   ssh root@159.65.241.127 "nano /root/platform-infra/.env"
   # Change IEOMD_IMAGE_TAG=latest to IEOMD_IMAGE_TAG=<previous-tag>
   ```
3. Pull and restart:
   ```bash
   ssh root@159.65.241.127 "cd /root/platform-infra && docker compose pull && docker compose up -d"
   ```

## Deploying a Fix

1. Build and push fixed image locally
2. Sync config if needed:
   ```bash
   rsync -avz --exclude='.git' --exclude='.env' repos/platform-infra/ root@159.65.241.127:/root/platform-infra/
   ```
3. Pull and restart on droplet:
   ```bash
   ssh root@159.65.241.127 "cd /root/platform-infra && docker compose pull && docker compose up -d"
   ```

## Monitoring

### Where errors go

- **IEOMD errors** → Matrix #sparkswarm-ops room (or Discord during transition)
- **Container crashes** → `docker logs`
- **System issues** → DigitalOcean monitoring dashboard

### What we intentionally don't fix immediately

- **Analytics gaps** - Umami being down for a few hours is fine
- **Minor UI glitches** - Can wait for next deploy
- **Non-critical feature bugs** - File an issue, fix in next sprint

### What needs immediate attention

- **Site completely down** - Users can't access secrets
- **Payment failures** - BTCPay not processing payments
- **Data loss risk** - Postgres issues, disk full
- **Security incidents** - Unusual access patterns, credential exposure

## CI Runners (do-1gb-runner-main)

Self-hosted GitHub Actions runners live on a separate droplet (`167.172.224.151`), not the production platform droplet.

### Runner not picking up jobs

```bash
# Check which runners are running
ssh root@167.172.224.151 "systemctl list-units --type=service --state=running | grep actions.runner"

# Restart a specific runner (e.g., spark-swarm)
ssh root@167.172.224.151 "systemctl restart 'actions.runner.richmiles-spark-swarm.*'"

# Check runner logs
ssh root@167.172.224.151 "journalctl -u 'actions.runner.richmiles-spark-swarm.*' --tail 50"

# Restart ALL runners
ssh root@167.172.224.151 "systemctl restart 'actions.runner.richmiles-*'"
```

### Runner offline in GitHub

If GitHub shows the runner as offline but systemd shows it running, the runner may have lost its registration token. Re-run the setup script:

```bash
# From your local machine
GITHUB_TOKEN=$(gh auth token) ssh root@167.172.224.151 "GITHUB_TOKEN='${GITHUB_TOKEN}' bash /root/setup-runners.sh"
```

### Disk full on runner droplet

Docker build caches can fill the disk. Clean up:

```bash
ssh root@167.172.224.151 "docker system prune -f && df -h /"
```

### Memory pressure

The runner droplet has 1GB RAM. If builds are OOM-killing, check usage:

```bash
ssh root@167.172.224.151 "free -h && ps aux --sort=-%mem | head 10"
```

Consider upgrading the droplet if this happens frequently.

## Emergency Contacts

- DigitalOcean Status: https://status.digitalocean.com
- Caddy Issues: Check https://github.com/caddyserver/caddy/issues
- BTCPay Issues: Check https://github.com/btcpayserver/btcpayserver/issues
