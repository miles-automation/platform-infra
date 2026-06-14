#!/usr/bin/env bash
# Idempotent setup for the platform-ci droplet. Run AS ROOT on the CI box, AFTER:
#   - /etc/platform-ci/env exists (secrets + config; see README — never in git)
#   - /srv/platform-ci/workspace/platform.toml exists (scp'd from the workspace; not in git)
# Re-runnable: installs only what's missing, then (re)starts the worker + Caddy.
set -euo pipefail

WORKSPACE=/srv/platform-ci/workspace
REPO_DIR="$WORKSPACE/repos/platform-infra"
INFRA_REMOTE="https://github.com/miles-automation/platform-infra.git"

echo "==> dirs"
mkdir -p "$WORKSPACE/repos" /srv/platform-ci/logs /etc/platform-ci
chmod 700 /etc/platform-ci

echo "==> apt deps (git curl ca-certificates jq nodejs npm)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates jq nodejs npm >/dev/null

echo "==> uv (for 'make check' / app builds)"
if ! command -v uv >/dev/null 2>&1; then
	curl -LsSf https://astral.sh/uv/install.sh | sh
	ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv
fi

echo "==> caddy"
if ! command -v caddy >/dev/null 2>&1; then
	apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' >/etc/apt/sources.list.d/caddy-stable.list
	apt-get update -qq
	apt-get install -y -qq caddy >/dev/null
fi

echo "==> platform-infra checkout at $REPO_DIR (main)"
if [ ! -d "$REPO_DIR/.git" ]; then
	git clone "$INFRA_REMOTE" "$REPO_DIR"
fi
git -C "$REPO_DIR" fetch origin -q
git -C "$REPO_DIR" checkout -q main
git -C "$REPO_DIR" pull -q --ff-only origin main

echo "==> systemd unit"
install -m 0644 "$REPO_DIR/ci/platform-ci.service" /etc/systemd/system/platform-ci.service
systemctl daemon-reload
systemctl enable platform-ci >/dev/null 2>&1 || true

echo "==> caddy config"
install -d /etc/caddy
install -m 0644 "$REPO_DIR/ci/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

echo "==> (re)start worker"
systemctl restart platform-ci
sleep 1
systemctl --no-pager --full status platform-ci | head -8 || true

echo "==> local health"
curl -fsS http://127.0.0.1:8765/healthz && echo " <- worker ok" || echo "worker not healthy yet"
