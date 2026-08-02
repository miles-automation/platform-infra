#!/usr/bin/env bash
# docker_prune.sh — standing disk guard for the fleet's docker hosts.
#
# Both droplets have filled their root disks with docker debris and broken deploys with
# cryptic ENOSPC errors (prod 2026-07-20, platform-ci 2026-08-02: a human-index-v2 build
# died inside npm). This runs on a timer (systemd/docker-prune.timer) and prunes:
#   - exited containers older than a day
#   - unused images older than IMAGE_UNTIL (anything used by a running container is always
#     kept; keeping recent unused images means the previous rollout tag survives for a few
#     days of fast local rollback)
#   - build cache older than BUILDER_UNTIL
# If the root filesystem is still at/above THRESHOLD_PCT after that gentle pass, it
# escalates to a full unused-image + build-cache prune — everything pruned is re-pullable
# from ghcr or re-buildable, so the only cost is a slower next build/rollback.
#
# NEVER prunes volumes: volumes are data (postgres), not debris.
set -euo pipefail

THRESHOLD_PCT="${DOCKER_PRUNE_THRESHOLD_PCT:-80}"
IMAGE_UNTIL="${DOCKER_PRUNE_IMAGE_UNTIL:-168h}"
BUILDER_UNTIL="${DOCKER_PRUNE_BUILDER_UNTIL:-72h}"

used_pct() { df --output=pcent / | tail -n1 | tr -dc '0-9'; }

echo "== docker-prune $(date -u +%Y-%m-%dT%H:%M:%SZ) start: $(used_pct)% used"
df -h / | tail -n1

docker container prune -f --filter "until=24h"
docker image prune -af --filter "until=${IMAGE_UNTIL}"
docker builder prune -af --filter "until=${BUILDER_UNTIL}"

pct="$(used_pct)"
if [ "$pct" -ge "$THRESHOLD_PCT" ]; then
	echo "== still ${pct}% used (>= ${THRESHOLD_PCT}%) — escalating to full unused prune"
	docker builder prune -af
	docker image prune -af
fi

echo "== docker-prune done: $(used_pct)% used"
df -h / | tail -n1
