#!/usr/bin/env bash
# Drift checker: compares running container images against pinned tags in .env
# Designed to run on the droplet as a systemd timer, alerts Matrix on drift.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"

# Load .env for Matrix credentials + image tags
if [[ -f "$ENV_FILE" ]]; then
  eval "$(
    python3 - "$ENV_FILE" <<'PY'
import pathlib, shlex, sys
path = pathlib.Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(f"export {key}={shlex.quote(value)}")
PY
  )"
fi

MATRIX_HOMESERVER_URL="${MATRIX_HOMESERVER_URL:-}"
MATRIX_ACCESS_TOKEN="${MATRIX_ACCESS_TOKEN:-}"
MATRIX_ROOM_ID="${MATRIX_ROOM_ID:-}"

log() { printf "[drift-check] %s\n" "$*"; }

send_matrix_alert() {
  local message="$1"
  if [[ -z "$MATRIX_HOMESERVER_URL" || -z "$MATRIX_ACCESS_TOKEN" || -z "$MATRIX_ROOM_ID" ]]; then
    return 0
  fi

  local room_id_encoded
  room_id_encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$MATRIX_ROOM_ID")"
  local txn_id="drift-check-$(date -u +%s)-$RANDOM"
  local body
  body="$(printf '{"msgtype":"m.text","body":"%s"}' "$(printf "%s" "$message" | sed 's/"/\\"/g')")"

  curl -fsS -X PUT \
    -H "Authorization: Bearer ${MATRIX_ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$body" \
    "${MATRIX_HOMESERVER_URL%/}/_matrix/client/v3/rooms/${room_id_encoded}/send/m.room.message/${txn_id}" \
    >/dev/null || true
}

# Service → image tag env var mapping (matches platform.toml convention)
declare -A SERVICE_TAG_MAP=(
  [ieomd]=IEOMD_IMAGE_TAG
  [noodle]=NOODLE_IMAGE_TAG
  [human-index]=HUMAN_INDEX_IMAGE_TAG
  [eshers-codex]=ESHERS_CODEX_IMAGE_TAG
  [richmiles-xyz]=RICHMILES_XYZ_IMAGE_TAG
  [bullshit-or-fit]=BULLSHIT_OR_FIT_IMAGE_TAG
  [spark-swarm]=SPARK_SWARM_IMAGE_TAG
  [miles-automation]=MILES_AUTOMATION_IMAGE_TAG
)

declare -A SERVICE_IMAGE_MAP=(
  [ieomd]=ghcr.io/miles-automation/ieomd-app
  [noodle]=ghcr.io/miles-automation/noodle-app
  [human-index]=ghcr.io/miles-automation/human-index-app
  [eshers-codex]=ghcr.io/miles-automation/eshers-codex-app
  [richmiles-xyz]=ghcr.io/miles-automation/richmiles-xyz-app
  [bullshit-or-fit]=ghcr.io/miles-automation/bullshit-or-fit
  [spark-swarm]=ghcr.io/miles-automation/spark-swarm
  [miles-automation]=ghcr.io/miles-automation/miles-automation-app
)

issues=()

# Check image drift: is the running container using the pinned tag?
for service in "${!SERVICE_TAG_MAP[@]}"; do
  tag_var="${SERVICE_TAG_MAP[$service]}"
  tag="${!tag_var:-latest}"
  image="${SERVICE_IMAGE_MAP[$service]}"
  desired="${image}:${tag}"

  # Get actual running image
  actual=$(cd "$ROOT_DIR" && docker compose -f "$COMPOSE_FILE" ps --format json 2>/dev/null \
    | python3 -c "
import json, sys
data = json.loads(sys.stdin.read() or '[]')
if isinstance(data, list):
    for row in data:
        if row.get('Service') == '$service':
            print(row.get('Image', ''))
            break
" 2>/dev/null || echo "")

  if [[ -z "$actual" ]]; then
    issues+=("service not running: $service")
  elif [[ "$actual" != "$desired" ]]; then
    issues+=("image drift: $service wants $desired but running $actual")
  fi
done

# Check for stopped/unhealthy containers
unhealthy=$(cd "$ROOT_DIR" && docker compose -f "$COMPOSE_FILE" ps --format json 2>/dev/null \
  | python3 -c "
import json, sys
data = json.loads(sys.stdin.read() or '[]')
if isinstance(data, list):
    for row in data:
        state = row.get('State', '')
        health = row.get('Health', '')
        service = row.get('Service', '')
        if state != 'running':
            print(f'{service}: state={state}')
        elif health and health != 'healthy' and health != '':
            print(f'{service}: health={health}')
" 2>/dev/null || echo "")

while IFS= read -r line; do
  [[ -n "$line" ]] && issues+=("$line")
done <<< "$unhealthy"

if (( ${#issues[@]} > 0 )); then
  drift_report="Drift detected ($(date -u +%Y-%m-%dT%H:%M:%SZ)):"
  for issue in "${issues[@]}"; do
    drift_report+=$'\n'"  - $issue"
    log "$issue"
  done
  send_matrix_alert "$drift_report"
  log "drift detected (${#issues[@]} issues), alert sent"
  exit 1
fi

log "ok: no drift detected"
