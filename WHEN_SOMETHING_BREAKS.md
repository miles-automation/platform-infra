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

Self-hosted GitHub Actions runners live on a separate droplet (`167.172.224.151`), not the production platform droplet. There is one **org-level** runner — `actions.runner.miles-automation.do-1gb-org-runner` — that services every `miles-automation/*` repo via the `do-1gb-canary` label. (The older per-repo `actions.runner.richmiles-*` services from `setup-runners.sh` are obsolete.)

### Workflow defaults (as of 2026-04-27)

All workflows now default to `ubuntu-latest` — `self-hosted-canary` is an opt-in `workflow_dispatch` choice. Runs land on the canary only when:
- A manual dispatch explicitly picks `runner_target: self-hosted-canary`, or
- A schedule/push targets a workflow that hasn't been migrated yet (none currently).

So if the runner is offline, **CI keeps working on ubuntu-latest** — there is no urgency to restart it. Don't restart it just to keep `do-1gb-canary` "available" if you're not actively using it.

### Diagnosing runner state

```bash
# What does GitHub think? (org-level)
gh api orgs/miles-automation/actions/runners

# Service status on the box
ssh root@167.172.224.151 "systemctl status actions.runner.miles-automation.do-1gb-org-runner.service --no-pager -l"

# Tail recent activity
ssh root@167.172.224.151 "journalctl -u actions.runner.miles-automation.do-1gb-org-runner.service -n 100 --no-pager"
```

### Runner failed (OOM-kill — common failure mode)

The droplet is 1GB RAM with no swap. A Docker build can push peak memory past the limit and the kernel OOM-killer terminates the runner. systemd marks the unit `failed` (not `crashed`), and the unit has no `Restart=on-failure`, so it stays down. Symptoms in `gh api orgs/miles-automation/actions/runners`: `total_count: 0`. Symptoms in `systemctl status`: `Active: failed (Result: oom-kill)`.

Recovery:

```bash
# 1) Kill stale leftover workers from the OOM (Runner.Worker, docker, docker-buildx, node — common after oom-kill)
ssh root@167.172.224.151 "pkill -9 -f 'Runner\\.Worker|docker-buildx' || true; sleep 2; ps aux | grep -E 'Runner|docker-buildx' | grep -v grep || echo '(clean)'"

# 2) Reset the failed state and start the service
ssh root@167.172.224.151 "systemctl reset-failed actions.runner.miles-automation.do-1gb-org-runner.service && systemctl start actions.runner.miles-automation.do-1gb-org-runner.service"

# 3) Confirm it registered with GitHub
gh api orgs/miles-automation/actions/runners
# Expect total_count >= 1, status "online"
```

If it OOMs again immediately, either reduce concurrent jobs or fix the underlying memory floor (add swap, resize droplet to s-1vcpu-2gb).

### Runner offline in GitHub but service is running

The runner may have lost its registration. Reconfigure (no re-installation):

```bash
# Get a fresh registration token
gh api -X POST orgs/miles-automation/actions/runners/registration-token --jq .token

# On the droplet, re-register (replaces existing registration)
ssh root@167.172.224.151
cd /opt/actions-runner-org
sudo -u runner ./config.sh remove --token <removal-token-from-above-call>  # or use --unattended with a fresh PAT
sudo -u runner ./config.sh --url https://github.com/miles-automation --token <reg-token> --name do-1gb-org-runner --labels self-hosted,linux,x64,do-1gb-canary,platform --unattended --replace
./svc.sh start
```

### Disk full on runner droplet

Docker build caches can fill the disk. Clean up:

```bash
ssh root@167.172.224.151 "docker system prune -af && df -h /"
```

### Memory pressure

The runner droplet has 1GB RAM **with no swap configured**. If builds are OOM-killing, check usage:

```bash
ssh root@167.172.224.151 "free -h && ps aux --sort=-%mem | head -n 10"
```

Mitigations (in increasing order of disruption):
1. Add a 1G swap file: `ssh root@167.172.224.151 "fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab"`
2. Add `Restart=on-failure` + `RestartSec=10s` to `/etc/systemd/system/actions.runner.miles-automation.do-1gb-org-runner.service` so the next OOM auto-recovers.
3. Resize droplet to s-1vcpu-2gb.

## Emergency Contacts

- DigitalOcean Status: https://status.digitalocean.com
- Caddy Issues: Check https://github.com/caddyserver/caddy/issues
- BTCPay Issues: Check https://github.com/btcpayserver/btcpayserver/issues
