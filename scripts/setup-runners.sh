#!/usr/bin/env bash
# setup-runners.sh — Register GitHub Actions self-hosted runners for all platform repos.
#
# Usage (run ON the droplet):
#   GITHUB_TOKEN=ghp_... ./setup-runners.sh
#
# Or generate tokens locally and pipe them:
#   ssh root@167.172.224.151 "GITHUB_TOKEN='$(gh auth token)' bash -s" < scripts/setup-runners.sh
#
# Idempotent: skips repos whose runner directory already contains a .runner config.

set -euo pipefail

RUNNER_VERSION="2.332.0"
RUNNER_TARBALL="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TARBALL}"
RUNNER_USER="runner"
LABELS="self-hosted,linux,x64,do-1gb-canary,platform"
RUNNER_NAME="$(hostname)"

# Map: short-name -> github-repo-name -> install-dir
declare -A REPOS=(
  [ieomd]="in-the-event-of-my-death"
  [noodle]="noodle"
  [human-index]="human-index"
  [richmiles-xyz]="richmiles.xyz"
  [bof]="bullshit-or-fit"
  [code-loom]="code-loom"
  [platform-infra]="platform-infra"
)

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "ERROR: GITHUB_TOKEN must be set (gh auth token or a PAT with admin scope)"
  exit 1
fi

# Download runner tarball if not cached
CACHE_DIR="/opt/actions-runner-cache"
mkdir -p "$CACHE_DIR"
if [[ ! -f "$CACHE_DIR/$RUNNER_TARBALL" ]]; then
  echo ">>> Downloading runner v${RUNNER_VERSION}..."
  curl -sL "$RUNNER_URL" -o "$CACHE_DIR/$RUNNER_TARBALL"
else
  echo ">>> Runner tarball cached at $CACHE_DIR/$RUNNER_TARBALL"
fi

register_runner() {
  local short_name="$1"
  local repo_name="$2"
  local install_dir="/opt/actions-runner-${short_name}"

  echo ""
  echo "=== ${repo_name} (${install_dir}) ==="

  # Skip if already registered
  if [[ -f "$install_dir/.runner" ]]; then
    echo "    Already registered, skipping."
    # Make sure the service is running
    local svc_pattern="actions.runner.richmiles-${repo_name}"
    if systemctl list-units --type=service --state=running 2>/dev/null | grep -q "$svc_pattern"; then
      echo "    Service is running."
    else
      echo "    Service not running, attempting to start..."
      cd "$install_dir"
      sudo ./svc.sh start || true
    fi
    return 0
  fi

  # Create directory and extract
  mkdir -p "$install_dir"
  tar xzf "$CACHE_DIR/$RUNNER_TARBALL" -C "$install_dir"
  chown -R "$RUNNER_USER:$RUNNER_USER" "$install_dir"

  # Get registration token from GitHub API
  echo "    Fetching registration token..."
  local reg_token
  reg_token=$(curl -s -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    "https://api.github.com/repos/richmiles/${repo_name}/actions/runners/registration-token" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

  if [[ -z "$reg_token" || "$reg_token" == "None" ]]; then
    echo "    ERROR: Failed to get registration token for ${repo_name}"
    return 1
  fi

  # Configure the runner (as the runner user)
  echo "    Configuring runner..."
  cd "$install_dir"
  sudo -u "$RUNNER_USER" ./config.sh \
    --url "https://github.com/richmiles/${repo_name}" \
    --token "$reg_token" \
    --name "$RUNNER_NAME" \
    --labels "$LABELS" \
    --work "_work" \
    --unattended \
    --replace

  # Install and start systemd service
  echo "    Installing systemd service..."
  ./svc.sh install "$RUNNER_USER"
  ./svc.sh start

  echo "    Done."
}

echo "Setting up GitHub Actions runners on $(hostname)"
echo "Runner version: ${RUNNER_VERSION}"
echo "Labels: ${LABELS}"
echo ""

for short_name in "${!REPOS[@]}"; do
  repo_name="${REPOS[$short_name]}"
  register_runner "$short_name" "$repo_name"
done

echo ""
echo "=== Summary ==="
systemctl list-units --type=service --state=running | grep actions.runner || echo "No runner services found"
echo ""
echo "Done. Verify runners at: gh api repos/richmiles/<repo>/actions/runners"
