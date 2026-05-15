#!/usr/bin/env bash
# Drift Deploy agent installer.
#
# The agent runs as a docker container, so the only host-side dependency
# is Docker itself. Works on Synology NAS (no systemd), Raspberry Pi,
# regular Linux VMs, anywhere Docker runs.
#
# Pipe-able from the control plane:
#   curl -sSL $CP_URL/agent/install.sh | \
#     DEVICE_NAME=<name> BOOTSTRAP_TOKEN=<token> \
#     CP_URL=$CP_URL GROUP_ID=<group> sudo -E bash
#
# DEVICE_NAME identifies this device in the control plane.
# GROUP_ID is the logical grouping (cloud/edge/client/...) — surfaced
# to compose bundles via ${DRIFT_GROUP_ID} so one bundle can label its
# metrics per device.
#
# Auth model: bearer-only. /drift/api/deploy/agent/* is not gated by
# Caddy basic_auth — the device's bootstrap token is the credential.

set -euo pipefail

: "${DEVICE_NAME:?DEVICE_NAME required}"
: "${BOOTSTRAP_TOKEN:?BOOTSTRAP_TOKEN required}"
: "${CP_URL:?CP_URL required}"
# Logical grouping for this device — surfaced to bundles as
# ${DRIFT_GROUP_ID}. Common values: cloud, edge, client-x, prod.
: "${GROUP_ID:?GROUP_ID required (e.g. GROUP_ID=cloud or GROUP_ID=edge)}"
POLL_INTERVAL=${POLL_INTERVAL:-30}

if [ "$(id -u)" != 0 ]; then
  echo "install.sh must run as root (use sudo -E)" >&2
  exit 1
fi

for c in docker curl tar; do
  command -v "$c" >/dev/null || { echo "missing required tool: $c" >&2; exit 1; }
done

mkdir -p /etc/drift-deploy /var/lib/drift-deploy/apps /var/lib/node_exporter/textfile_collector

umask 077
cat > /etc/drift-deploy/env <<EOF
DEVICE_NAME=$DEVICE_NAME
BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
CP_URL=$CP_URL
POLL_INTERVAL=$POLL_INTERVAL
GROUP_ID=$GROUP_ID
EOF
chmod 600 /etc/drift-deploy/env
umask 022

# Fetch the build context from the control plane and build the agent
# image locally. Tiny context (Dockerfile + ~6KB script); alpine base
# pulls fast. Each install gets a fresh build — no registry needed.
echo "fetching agent build context..."
CTX=$(mktemp -d)
curl -fsSL "$CP_URL/agent/build-context.tar" -o "$CTX/ctx.tar"
mkdir -p "$CTX/build"
tar -xf "$CTX/ctx.tar" -C "$CTX/build"
echo "building drift-deploy-agent image..."
docker build -t drift-deploy-agent:latest "$CTX/build"
rm -rf "$CTX"

# Replace any prior agent container (idempotent reinstall).
if docker ps -a --format '{{.Names}}' | grep -qx drift-deploy-agent; then
  echo "removing prior agent container..."
  docker rm -f drift-deploy-agent
fi

echo "starting drift-deploy-agent..."
docker run -d \
  --name drift-deploy-agent \
  --restart unless-stopped \
  --env-file /etc/drift-deploy/env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /var/lib/drift-deploy:/var/lib/drift-deploy \
  -v /var/lib/node_exporter/textfile_collector:/var/lib/node_exporter/textfile_collector \
  drift-deploy-agent:latest

echo
echo "✓ installed. status:"
docker ps --filter name=drift-deploy-agent --format '{{.Names}}\t{{.Status}}'
echo
echo "tail with:  docker logs -f drift-deploy-agent"
echo "to upgrade: re-run this install.sh; the prior container is replaced in place."
