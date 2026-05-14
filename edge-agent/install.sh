#!/usr/bin/env bash
# Drift Deploy agent installer.
#
# Pipe-able from the control plane:
#   curl -sSL -u 'drift:CADDY_PW' $CP_URL/agent/install.sh | \
#     DEVICE_NAME=<name> BOOTSTRAP_TOKEN=<token> \
#     CP_URL=$CP_URL CP_BASIC_AUTH='drift:CADDY_PW' \
#     MANAGED_APPS=podnot,ente sudo -E bash

set -euo pipefail

: "${DEVICE_NAME:?DEVICE_NAME required}"
: "${BOOTSTRAP_TOKEN:?BOOTSTRAP_TOKEN required}"
: "${CP_URL:?CP_URL required}"
: "${CP_BASIC_AUTH:?CP_BASIC_AUTH required}"
MANAGED_APPS=${MANAGED_APPS:-}
POLL_INTERVAL=${POLL_INTERVAL:-30}

if [ "$(id -u)" != 0 ]; then
  echo "install.sh must run as root (use sudo -E)" >&2
  exit 1
fi

# Tooling deps. We don't pull docker here — assume it's already configured.
need=()
for c in curl jq tar sha256sum flock; do
  command -v "$c" >/dev/null || need+=("$c")
done
if [ ${#need[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null; then
    apt-get update -qq && apt-get install -y -qq "${need[@]}"
  else
    echo "missing required tools: ${need[*]} (no apt-get available; install manually)" >&2
    exit 1
  fi
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "docker and 'docker compose' (v2) must be installed" >&2
  exit 1
fi

mkdir -p /etc/drift-deploy /var/lib/drift-deploy/apps

umask 077
cat > /etc/drift-deploy/env <<EOF
DEVICE_NAME=$DEVICE_NAME
BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
CP_URL=$CP_URL
CP_BASIC_AUTH=$CP_BASIC_AUTH
MANAGED_APPS=$MANAGED_APPS
POLL_INTERVAL=$POLL_INTERVAL
EOF
chmod 600 /etc/drift-deploy/env
umask 022

curl -fsSL -u "$CP_BASIC_AUTH" "$CP_URL/agent/agent.sh" -o /usr/local/bin/drift-deploy-agent.sh
chmod +x /usr/local/bin/drift-deploy-agent.sh

curl -fsSL -u "$CP_BASIC_AUTH" "$CP_URL/agent/drift-deploy-agent.service" \
     -o /etc/systemd/system/drift-deploy-agent.service

systemctl daemon-reload
systemctl enable --now drift-deploy-agent

echo
echo "✓ installed. status:"
systemctl --no-pager --lines=0 status drift-deploy-agent || true
echo
echo "tail with:  journalctl -u drift-deploy-agent -f"
