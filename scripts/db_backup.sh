#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_DIR="${COMPOSE_DIR:-$ROOT_DIR}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"

if [[ -f "$ENV_FILE" ]]; then
  eval "$(
    python3 - "$ENV_FILE" <<'PY'
import pathlib
import shlex
import sys

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

BACKUP_DIR="${DB_BACKUP_LOCAL_DIR:-/root/backups}"
SPACES_PREFIX="${DB_BACKUP_SPACES_PREFIX:-postgres}"
SPACES_BUCKET="${SPACES_BUCKET:-}"
SPACES_REGION="${SPACES_REGION:-nyc3}"
SPACES_ENDPOINT="${SPACES_ENDPOINT:-https://${SPACES_REGION}.digitaloceanspaces.com}"
AWS_CLI_IMAGE="${DB_BACKUP_AWS_CLI_IMAGE:-amazon/aws-cli:latest}"
HEALTH_MAX_AGE_HOURS="${DB_BACKUP_HEALTH_MAX_AGE_HOURS:-25}"
STATE_FILE="${BACKUP_DIR}/backup_state.env"
DATABASE_USER="${DB_BACKUP_DATABASE_USER:-${POSTGRES_USER:-postgres}}"

# All databases to back up (derived from init-db.sql).
# Override with DB_BACKUP_DATABASES (comma-separated) if needed.
DEFAULT_DATABASES="ieomd_db,umami_db,synapse_db,human_index_db,spark_swarm_db,eshers_codex_db,noodle_db"
IFS=',' read -ra DATABASES <<< "${DB_BACKUP_DATABASES:-$DEFAULT_DATABASES}"

MATRIX_HOMESERVER_URL="${MATRIX_HOMESERVER_URL:-}"
MATRIX_ACCESS_TOKEN="${MATRIX_ACCESS_TOKEN:-}"
MATRIX_ROOM_ID="${MATRIX_ROOM_ID:-}"

export AWS_ACCESS_KEY_ID="${SPACES_ACCESS_KEY:-${AWS_ACCESS_KEY_ID:-}}"
export AWS_SECRET_ACCESS_KEY="${SPACES_SECRET_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"
export AWS_DEFAULT_REGION="${SPACES_REGION}"

log() {
  printf "[db-backup] %s\n" "$*"
}

require_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    log "missing required tool: $tool"
    exit 1
  fi
}

aws_cmd() {
  docker run --rm \
    -e AWS_ACCESS_KEY_ID \
    -e AWS_SECRET_ACCESS_KEY \
    -e AWS_DEFAULT_REGION \
    -v "${BACKUP_DIR}:${BACKUP_DIR}" \
    "$AWS_CLI_IMAGE" \
    --endpoint-url "$SPACES_ENDPOINT" \
    "$@"
}

to_epoch_date() {
  local date_value="$1"
  if date -u -d "$date_value" +%s >/dev/null 2>&1; then
    date -u -d "$date_value" +%s
    return 0
  fi
  date -u -j -f "%Y-%m-%d" "$date_value" +%s
}

day_of_week() {
  local date_value="$1"
  if date -u -d "$date_value" +%u >/dev/null 2>&1; then
    date -u -d "$date_value" +%u
    return 0
  fi
  date -u -j -f "%Y-%m-%d" "$date_value" +%u
}

to_epoch_iso() {
  local iso_value="$1"
  if date -u -d "$iso_value" +%s >/dev/null 2>&1; then
    date -u -d "$iso_value" +%s
    return 0
  fi
  date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$iso_value" +%s
}

persist_state() {
  local backup_at="$1"
  local upload_status="$2"
  local upload_at="$3"
  local backup_file="$4"
  local remote_key="$5"
  local error_text="$6"

  mkdir -p "$BACKUP_DIR"
  local tmp_state="${STATE_FILE}.tmp"
  cat >"$tmp_state" <<EOF
LAST_BACKUP_AT=${backup_at}
LAST_UPLOAD_STATUS=${upload_status}
LAST_UPLOAD_AT=${upload_at}
LAST_BACKUP_FILE=${backup_file}
LAST_REMOTE_KEY=${remote_key}
LAST_ERROR=${error_text}
EOF
  mv "$tmp_state" "$STATE_FILE"
}

send_matrix_alert() {
  local message="$1"
  if [[ -z "$MATRIX_HOMESERVER_URL" || -z "$MATRIX_ACCESS_TOKEN" || -z "$MATRIX_ROOM_ID" ]]; then
    return 0
  fi

  local room_id_encoded
  room_id_encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$MATRIX_ROOM_ID")"
  local txn_id="db-backup-$(date -u +%s)-$RANDOM"
  local body
  body="$(printf '{"msgtype":"m.text","body":"%s"}' "$(printf "%s" "$message" | sed 's/"/\\"/g')")"

  curl -fsS -X PUT \
    -H "Authorization: Bearer ${MATRIX_ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$body" \
    "${MATRIX_HOMESERVER_URL%/}/_matrix/client/v3/rooms/${room_id_encoded}/send/m.room.message/${txn_id}" \
    >/dev/null || true
}

cleanup_local() {
  local db_name="$1"
  find "$BACKUP_DIR" -maxdepth 1 -type f -name "${db_name}_*.sql.gz" -mtime +7 -delete
}

cleanup_remote() {
  local db_name="$1"
  local now_epoch
  now_epoch="$(date -u +%s)"
  local prefix="${SPACES_PREFIX%/}/"

  mapfile -t keys < <(
    aws_cmd s3api list-objects-v2 \
      --bucket "$SPACES_BUCKET" \
      --prefix "$prefix" \
      --query 'Contents[].Key' \
      --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d'
  )

  local key
  for key in "${keys[@]}"; do
    local base
    base="$(basename "$key")"
    if [[ ! "$base" =~ ^${db_name}_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}\.sql\.gz$ ]]; then
      continue
    fi

    local date_part
    date_part="${base#${db_name}_}"
    date_part="${date_part%%_*}"

    local backup_epoch
    if ! backup_epoch="$(to_epoch_date "$date_part" 2>/dev/null)"; then
      continue
    fi

    local age_days
    age_days=$(((now_epoch - backup_epoch) / 86400))
    if (( age_days <= 30 )); then
      continue
    fi

    local weekday
    weekday="$(day_of_week "$date_part")"
    if (( weekday == 7 && age_days <= 90 )); then
      continue
    fi

    aws_cmd s3 rm "s3://${SPACES_BUCKET}/${key}" --only-show-errors
  done
}

backup_single_db() {
  local db_name="$1"
  local timestamp="$2"
  local now_iso="$3"

  local filename="${db_name}_${timestamp}.sql.gz"
  local tmp_path="${BACKUP_DIR}/.${filename}.tmp"
  local local_path="${BACKUP_DIR}/${filename}"
  local object_key="${SPACES_PREFIX%/}/${filename}"

  log "starting backup for database=${db_name}"
  if ! (
    cd "$COMPOSE_DIR"
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
      pg_dump -U "$DATABASE_USER" --no-owner --no-privileges "$db_name"
  ) | gzip -c >"$tmp_path"; then
    rm -f "$tmp_path"
    log "pg_dump failed for ${db_name}"
    send_matrix_alert "DB backup failed: pg_dump failed for ${db_name}."
    return 1
  fi

  mv "$tmp_path" "$local_path"
  log "backup written to ${local_path}"

  if ! aws_cmd s3 cp "$local_path" "s3://${SPACES_BUCKET}/${object_key}" --only-show-errors; then
    log "upload failed for ${db_name}"
    send_matrix_alert "DB backup upload failed for ${filename}."
    return 1
  fi

  aws_cmd s3api head-object --bucket "$SPACES_BUCKET" --key "$object_key" >/dev/null
  cleanup_local "$db_name"
  cleanup_remote "$db_name"
  log "backup completed and verified: s3://${SPACES_BUCKET}/${object_key}"
  return 0
}

run_backup() {
  require_tool docker
  require_tool gzip

  if [[ -z "$SPACES_BUCKET" || -z "$AWS_ACCESS_KEY_ID" || -z "$AWS_SECRET_ACCESS_KEY" ]]; then
    log "SPACES_BUCKET, SPACES_ACCESS_KEY, and SPACES_SECRET_KEY must be set"
    exit 1
  fi

  mkdir -p "$BACKUP_DIR"
  local timestamp now_iso
  timestamp="$(date -u +%Y-%m-%d_%H%M%S)"
  now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  local failed_dbs=()
  local succeeded_dbs=()

  for db_name in "${DATABASES[@]}"; do
    if backup_single_db "$db_name" "$timestamp" "$now_iso"; then
      succeeded_dbs+=("$db_name")
    else
      failed_dbs+=("$db_name")
    fi
  done

  # Persist state for health-check (tracks overall status)
  if (( ${#failed_dbs[@]} > 0 )); then
    persist_state "$now_iso" "partial" "$now_iso" "" "" "failed: ${failed_dbs[*]}"
    send_matrix_alert "DB backup partially failed. Succeeded: ${succeeded_dbs[*]:-none}. Failed: ${failed_dbs[*]}."
    exit 1
  fi

  persist_state "$now_iso" "success" "$now_iso" "" "" ""
  log "all ${#DATABASES[@]} databases backed up successfully"
}

run_health_check() {
  require_tool curl
  mkdir -p "$BACKUP_DIR"

  if [[ ! -f "$STATE_FILE" ]]; then
    send_matrix_alert "DB backup health check failed: state file missing (${STATE_FILE})."
    log "state file missing"
    exit 1
  fi

  # shellcheck disable=SC1090
  source "$STATE_FILE"

  if [[ "${LAST_UPLOAD_STATUS:-}" != "success" ]]; then
    send_matrix_alert "DB backup unhealthy: last status=${LAST_UPLOAD_STATUS:-unknown}. ${LAST_ERROR:-}"
    log "last upload status is not success"
    exit 1
  fi

  local last_backup_epoch now_epoch max_age_seconds age_seconds
  if ! last_backup_epoch="$(to_epoch_iso "${LAST_BACKUP_AT:-}" 2>/dev/null)"; then
    send_matrix_alert "DB backup health check failed: invalid LAST_BACKUP_AT (${LAST_BACKUP_AT:-unset})."
    log "invalid LAST_BACKUP_AT"
    exit 1
  fi

  now_epoch="$(date -u +%s)"
  max_age_seconds=$((HEALTH_MAX_AGE_HOURS * 3600))
  age_seconds=$((now_epoch - last_backup_epoch))
  if (( age_seconds > max_age_seconds )); then
    send_matrix_alert "DB backup stale: last backup at ${LAST_BACKUP_AT} (>${HEALTH_MAX_AGE_HOURS}h old). Databases: ${DATABASES[*]}"
    log "backup is stale"
    exit 1
  fi

  log "health check ok (${#DATABASES[@]} databases)"
}

case "$MODE" in
  run)
    run_backup
    ;;
  health-check)
    run_health_check
    ;;
  *)
    echo "Usage: $0 [run|health-check]"
    exit 1
    ;;
esac
