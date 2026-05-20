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

# Discover the host's actual Docker data dir. On vanilla Linux this is
# /var/lib/docker; on Synology DSM it's /volume1/@docker. Bundles that
# need cAdvisor-style image/layer visibility reference this via
# ${DRIFT_DOCKER_DATA_DIR} so a single bundle deploys cleanly across
# heterogeneous hosts.
DRIFT_DOCKER_DATA_DIR=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
echo "detected docker data dir: $DRIFT_DOCKER_DATA_DIR"

# Detect the host's actual upstream DNS resolvers so the container
# inherits resolvers that work from inside its network namespace. The
# default Docker behavior is to copy /etc/resolv.conf into the container,
# but on systemd-resolved boxes (most modern Linux, Jetson included)
# that file points at 127.0.0.53 — a stub listener that exists only on
# the host. Containers can't reach it. Result: every DNS lookup from
# inside the container times out, silently bricking the agent's check-ins
# while leaving long-lived TCP connections (e.g. vmagent shipping
# metrics) unaffected. Auto-detecting the real upstreams once at install
# time and passing them via --dns avoids the daemon.json detour entirely.
detect_dns() {
  # Prefer systemd-resolved's "Current DNS Server" line, then fall back
  # to /etc/resolv.conf with 127.0.0.x stubs filtered out.
  local servers=""
  if command -v resolvectl >/dev/null 2>&1; then
    servers=$(resolvectl status 2>/dev/null \
      | awk '/^\s*DNS Servers:/ { for (i=3; i<=NF; i++) print $i }' \
      | tr '\n' ' ')
  fi
  if [ -z "$servers" ] && [ -r /etc/resolv.conf ]; then
    servers=$(awk '/^nameserver/ && $2 !~ /^127\./ {print $2}' /etc/resolv.conf | tr '\n' ' ')
  fi
  echo "$servers"
}
DNS_SERVERS=$(detect_dns)
DNS_ARGS=""
if [ -n "$DNS_SERVERS" ]; then
  echo "detected host DNS resolvers: $DNS_SERVERS"
  for d in $DNS_SERVERS; do DNS_ARGS="$DNS_ARGS --dns $d"; done
else
  echo "no usable host DNS resolvers detected; relying on Docker defaults" >&2
fi

mkdir -p /etc/drift-deploy /var/lib/drift-deploy/apps /var/lib/node_exporter/textfile_collector

umask 077
cat > /etc/drift-deploy/env <<EOF
DEVICE_NAME=$DEVICE_NAME
BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
CP_URL=$CP_URL
POLL_INTERVAL=$POLL_INTERVAL
GROUP_ID=$GROUP_ID
DRIFT_DOCKER_DATA_DIR=$DRIFT_DOCKER_DATA_DIR
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
# --network host puts the agent in the host's network namespace, which:
#   - lets collect_facts() see the host's real interfaces + IPs (without
#     this, `ip addr` only shows the container's docker0 bridge IP)
#   - lets `hostname` return the host's hostname instead of the
#     container ID
#   - inherits the host's resolv.conf directly, so the corp-DNS-blocks-
#     1.1.1.1 issue we hit on the jetson never recurs (no need for
#     /etc/docker/daemon.json --dns workaround)
# The agent doesn't bind any ports, so network isolation isn't doing
# real work for us anyway. The $DNS_ARGS computed above only applies to
# the bridge-network fallback path; --network host bypasses it.
#
# /etc/os-release is bind-mounted from the host so collect_facts() can
# read the host's OS string instead of the alpine container's.
# shellcheck disable=SC2086
docker run -d \
  --name drift-deploy-agent \
  --restart unless-stopped \
  --network host \
  --env-file /etc/drift-deploy/env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /var/lib/drift-deploy:/var/lib/drift-deploy \
  -v /var/lib/node_exporter/textfile_collector:/var/lib/node_exporter/textfile_collector \
  -v /etc/os-release:/host/etc/os-release:ro \
  drift-deploy-agent:latest

echo
echo "✓ installed. status:"
docker ps --filter name=drift-deploy-agent --format '{{.Names}}\t{{.Status}}'
echo
echo "tail with:  docker logs -f drift-deploy-agent"
echo "to upgrade: re-run this install.sh; the prior container is replaced in place."
