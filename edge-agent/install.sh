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

# ---- summary + confirm ----
# Show the operator exactly what's about to happen before any side
# effects. Important: this script ends up running as `sudo -E bash`
# piped from curl, so stdin is the script body — read from /dev/tty.
cat <<SUMMARY

═══════════════════════════════════════════════════════════════════════
  drift-deploy-agent installer

  This script installs the Drift Deploy edge-agent — a small container
  that polls the Drift control plane (CP) and applies whatever app
  bundles you assign to this device via the Drift web UI or chat. The
  CP is the Drift server you stood up earlier (control plane = the
  observability + fleet-management backend); it tells this device what
  to run, and this device reports back its state + metrics + logs.

  Device identity
      name:           $DEVICE_NAME
      group:          $GROUP_ID
      control plane:  $CP_URL
      poll interval:  ${POLL_INTERVAL}s

  Host changes (require root)
      • Create system user 'drift' (sudo group, home /home/drift) for
        web-terminal access. Password is freshly generated and printed
        at the end of install. If 'drift' already exists, its password
        is NOT touched — rerun is for upgrading the agent only.
      • Write /etc/drift-deploy/env (mode 600 — holds the bootstrap
        token and CP URL).
      • Create /var/lib/drift-deploy/apps (bundle state) and
        /var/lib/node_exporter/textfile_collector (agent self-metrics).

  Container (drift-deploy-agent)
      • Built locally from a build context fetched from the CP.
      • --network host  (talks to the CP and any deployed apps).
      • --pid host      (needed to nsenter into PAM for the web-terminal
                         feature — root-equivalent privilege).
      • cap-add SYS_ADMIN + SYS_PTRACE  (same reason).
      • Mounts /var/run/docker.sock so it can 'docker compose up' on
        this host. Functionally equivalent to root on this box.
      • Restart policy: unless-stopped.

  Runtime
      • Polls the CP every ${POLL_INTERVAL}s for assigned apps.
      • Pulls + applies bundles as the CP's state changes.
      • Self-updates when the CP advertises a newer agent SHA — the
        new agent script replaces the current one on next restart.

═══════════════════════════════════════════════════════════════════════
SUMMARY

if [ -n "${DRIFT_INSTALL_ASSUME_YES:-}" ]; then
  echo "DRIFT_INSTALL_ASSUME_YES set — proceeding without prompt"
else
  ans=""
  # When the script comes in via `curl | sudo -E bash`, stdin is the
  # piped script body, not a TTY — read from /dev/tty instead.
  if [ -t 0 ]; then
    read -rp "Proceed with install? [y/N] " ans
  elif [ -e /dev/tty ]; then
    read -rp "Proceed with install? [y/N] " ans < /dev/tty
  else
    echo
    echo "ERROR: no TTY available for confirmation prompt." >&2
    echo "       Re-run with DRIFT_INSTALL_ASSUME_YES=1 to skip the prompt," >&2
    echo "       e.g. inside CI or another non-interactive context." >&2
    exit 1
  fi
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 0 ;;
  esac
fi
echo

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
# Explicit chmod for the textfile collector dir — host umask on
# Synology DSM (and some other distros) leaves mkdir at mode 700,
# which blocks the node-exporter container (uid 65534) from reading
# the agent's published .prom file. 755 is safe: nothing secret in
# this directory, contents are exporter-format counters.
chmod 755 /var/lib/node_exporter/textfile_collector

# ---- drift user (terminal access) ----
# Provision a host-side `drift` account for remote terminal sessions.
# Every web-terminal session lands at `/bin/login` and the operator
# authenticates as this user; member of the host's sudoers group, so
# `sudo` with this same password gives root. Password is generated
# fresh per device and printed once at the end of install.
#
# Idempotent: if `drift` already exists, leave its password alone — a
# rerun is for upgrading the agent, not rotating credentials. Operator
# can manually `passwd drift` on the device to rotate.
SUDO_GROUP=""
for g in sudo wheel administrators; do
  if getent group "$g" >/dev/null 2>&1; then
    SUDO_GROUP="$g"
    break
  fi
done
if [ -z "$SUDO_GROUP" ]; then
  echo "WARNING: no sudoers-style group (sudo/wheel/administrators) found on this host." >&2
  echo "         drift user will be created without sudo rights — host shell will work" >&2
  echo "         but privileged commands will fail." >&2
fi

DRIFT_USER_PASSWORD_GENERATED=""
DRIFT_USER_PROVISIONED=0
# Helper: pick the best-available shell. DSM only ships ash/sh; standard
# Linux distros have bash. Used both for new users and for fixing
# /sbin/nologin defaults on DSM.
_pick_shell() {
  for s in /bin/bash /bin/ash /bin/sh; do
    if [ -x "$s" ]; then echo "$s"; return; fi
  done
  echo /bin/sh
}
DRIFT_USER_SHELL=$(_pick_shell)

if id drift >/dev/null 2>&1; then
  echo "drift user already exists; leaving password unchanged (use 'passwd drift' to rotate)"
  DRIFT_USER_PROVISIONED=1
  # Best-effort sudoers group membership. usermod may not exist on
  # Synology — skip silently rather than aborting.
  if [ -n "$SUDO_GROUP" ] \
     && command -v usermod >/dev/null 2>&1 \
     && ! id -nG drift | tr ' ' '\n' | grep -qx "$SUDO_GROUP"; then
    usermod -aG "$SUDO_GROUP" drift && echo "added drift to '$SUDO_GROUP' group" || true
  fi
elif command -v useradd >/dev/null 2>&1; then
  # Standard Linux. /dev/urandom → base64 → strip /+= → 16 chars; avoids
  # shell-escape pitfalls when the operator pastes the value at login.
  DRIFT_USER_PASSWORD_GENERATED=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 16)
  useradd -m -s "$DRIFT_USER_SHELL" drift
  echo "drift:$DRIFT_USER_PASSWORD_GENERATED" | chpasswd
  if [ -n "$SUDO_GROUP" ] && command -v usermod >/dev/null 2>&1; then
    usermod -aG "$SUDO_GROUP" drift || true
  fi
  echo "created drift user (shell=$DRIFT_USER_SHELL${SUDO_GROUP:+, group=$SUDO_GROUP})"
  DRIFT_USER_PROVISIONED=1
else
  # Synology DSM (and anything else without useradd). We tried plumbing
  # the web terminal through nsenter+/bin/login here, but DSM's
  # customized login binary exits silently outside a getty context, so
  # password auth never completes. Rather than ship a feature that
  # half-works, skip the drift user entirely and tell the operator to
  # use DSM's native SSH for shell access on this device. Everything
  # else about the agent (reconcile, fleet metrics, deploys, registry
  # creds, observability) works fine.
  echo "WARNING: no useradd on this host (likely Synology DSM)." >&2
  echo "         Drift web terminal is not supported here — use DSM's own SSH" >&2
  echo "         for shell access. Skipping drift user creation." >&2
fi

# Probe-before-write: verify the supplied BOOTSTRAP_TOKEN actually
# authenticates against the CP BEFORE we overwrite /etc/drift-deploy/env.
# Without this guard, a wrong token (saved from an old install, typo,
# copy-paste from another device) silently replaces the working token in
# the env file, and the failure only surfaces hours later when check-ins
# start 401'ing in a docker restart spiral — exactly the failure mode
# that bit home-pi4-001 (a token mismatch that took 20+ hours of
# debugging to localize).
echo "verifying BOOTSTRAP_TOKEN against $CP_URL..."
probe_http=$(curl -sS -o /tmp/drift-install-probe.json -w '%{http_code}' \
  -H "Authorization: Bearer $BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  --connect-timeout 5 --max-time 15 \
  -X POST "$CP_URL/agent/check-in" \
  -d "{\"device_name\":\"$DEVICE_NAME\",\"agent_version\":\"install-probe\"}" 2>/dev/null) || probe_http="000"
if [ "$probe_http" != "200" ]; then
  echo >&2
  echo "ERROR: BOOTSTRAP_TOKEN does not authenticate for device '$DEVICE_NAME'." >&2
  echo "       CP returned HTTP $probe_http." >&2
  if [ "$probe_http" = "401" ] || [ "$probe_http" = "403" ]; then
    body=$(jq -r '.detail // "(no detail)"' /tmp/drift-install-probe.json 2>/dev/null \
            || cat /tmp/drift-install-probe.json 2>/dev/null \
            || echo "(no body)")
    echo "       Detail: $body" >&2
    echo "" >&2
    echo "Likely causes:" >&2
    echo "  - The token is stale: the device was deleted + re-commissioned" >&2
    echo "    on the CP and you're using an old install command." >&2
    echo "  - The DEVICE_NAME doesn't match the row in the CP's devices table." >&2
    echo "  - The token was edited / truncated since the original commission." >&2
    echo "" >&2
    echo "Fix: get a fresh install_cmd from Drift (commission_device or the" >&2
    echo "admin API) and re-run that command on this host. The existing" >&2
    echo "env file at /etc/drift-deploy/env is left untouched." >&2
  elif [ "$probe_http" = "000" ]; then
    echo "       (network failure — DNS or CP unreachable)" >&2
  fi
  rm -f /tmp/drift-install-probe.json
  exit 1
fi
rm -f /tmp/drift-install-probe.json
echo "✓ token authenticated"

# Detect host CA bundle BEFORE writing the env file so CURL_CA_BUNDLE
# (if applicable) lands in the same file the container loads via
# --env-file. The bind-mount that backs this path is added to the
# docker run invocation further down.
CA_BUNDLE_HOST=""
for path in \
  /etc/ssl/certs/ca-certificates.crt \
  /etc/pki/tls/certs/ca-bundle.crt \
  /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
  /etc/ssl/cert.pem \
; do
  if [ -r "$path" ]; then
    CA_BUNDLE_HOST="$path"
    break
  fi
done
CA_BUNDLE_MOUNT=""
CURL_CA_BUNDLE_ENVLINE=""
SSL_CERT_FILE_ENVLINE=""
DRIFT_HOST_CA_BUNDLE_ENVLINE=""
if [ -n "$CA_BUNDLE_HOST" ]; then
  echo "detected host CA bundle: $CA_BUNDLE_HOST"
  echo "  → mounted into agent at /host/etc/ssl/host-ca-bundle.crt"
  echo "  → CURL_CA_BUNDLE + SSL_CERT_FILE set so agent's curl AND python ssl trust it"
  echo "  → exposed to deployed apps as DRIFT_HOST_CA_BUNDLE=$CA_BUNDLE_HOST"
  CA_BUNDLE_MOUNT="-v $CA_BUNDLE_HOST:/host/etc/ssl/host-ca-bundle.crt:ro"
  # Three env vars covering both stdlib paths the agent uses:
  # - CURL_CA_BUNDLE: curl/openssl (the agent script's check-ins + the
  #   self-update bootstrap + every `docker compose pull`).
  # - SSL_CERT_FILE: python's `ssl` module (terminal-bridge.py's wss://
  #   connection back to the CP — websockets uses ssl.create_default_context
  #   which honors this, not CURL_CA_BUNDLE).
  # - DRIFT_HOST_CA_BUNDLE: HOST path, passed through compose so the
  #   docker daemon can bind-mount the file into app containers.
  CURL_CA_BUNDLE_ENVLINE="CURL_CA_BUNDLE=/host/etc/ssl/host-ca-bundle.crt"
  SSL_CERT_FILE_ENVLINE="SSL_CERT_FILE=/host/etc/ssl/host-ca-bundle.crt"
  DRIFT_HOST_CA_BUNDLE_ENVLINE="DRIFT_HOST_CA_BUNDLE=$CA_BUNDLE_HOST"
else
  echo "note: no host CA bundle found at standard locations; agent will rely on container's Mozilla bundle"
fi

umask 077
cat > /etc/drift-deploy/env <<EOF
DEVICE_NAME=$DEVICE_NAME
BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
CP_URL=$CP_URL
POLL_INTERVAL=$POLL_INTERVAL
GROUP_ID=$GROUP_ID
DRIFT_DOCKER_DATA_DIR=$DRIFT_DOCKER_DATA_DIR
$CURL_CA_BUNDLE_ENVLINE
$SSL_CERT_FILE_ENVLINE
$DRIFT_HOST_CA_BUNDLE_ENVLINE
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
# read the host's OS string. The mount is CONDITIONAL: Synology DSM
# and a few other non-systemd Linux variants don't ship it at all, and
# Docker would refuse to start the container if the source path
# doesn't exist. When absent, the agent falls back to "unknown" for
# the os facts field, which is acceptable on those hosts.
# OS-info source files — bind-mount each conditionally so the agent's
# collect_facts() can read host-side identity. Each source is tried in
# the order below until one yields a usable value; missing sources are
# fine (Synology DSM, for example, doesn't ship /etc/os-release at all).
OS_INFO_MOUNTS=""
for src in /etc/os-release /usr/lib/os-release /etc.defaults/VERSION /etc/lsb-release; do
  if [ -r "$src" ]; then
    # Use the same path inside the container under /host/ — keep the
    # source layout identical so collect_facts() can probe each spot.
    OS_INFO_MOUNTS="$OS_INFO_MOUNTS -v $src:/host${src}:ro"
  fi
done
if [ -z "$OS_INFO_MOUNTS" ]; then
  echo "note: no recognized OS-info files on this host; agent will report os=unknown"
fi

# shellcheck disable=SC2086
# --pid host + SYS_ADMIN/SYS_PTRACE: let the agent `nsenter -t 1 -m -p -u`
# into the host's namespaces to spawn `/bin/login` for web-terminal
# sessions. This is a real privilege bump from the prior minimal flags
# but doesn't expand the trust boundary: the agent already gets root-
# equivalent power via /var/run/docker.sock (anyone who can talk to the
# docker daemon can mount / and become root). Without these, the
# terminal feature can't reach the host's PAM stack.
docker run -d \
  --name drift-deploy-agent \
  --restart unless-stopped \
  --network host \
  --pid host \
  --cap-add SYS_ADMIN \
  --cap-add SYS_PTRACE \
  --env-file /etc/drift-deploy/env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /var/lib/drift-deploy:/var/lib/drift-deploy \
  -v /var/lib/node_exporter/textfile_collector:/var/lib/node_exporter/textfile_collector \
  $OS_INFO_MOUNTS \
  $CA_BUNDLE_MOUNT \
  drift-deploy-agent:latest

echo
echo "✓ installed. status:"
docker ps --filter name=drift-deploy-agent --format '{{.Names}}\t{{.Status}}'
echo
echo "tail with:  docker logs -f drift-deploy-agent"
echo "to upgrade: re-run this install.sh; the prior container is replaced in place."

if [ -n "$DRIFT_USER_PASSWORD_GENERATED" ]; then
  echo
  echo "===================================================================="
  echo "  drift user password (save this — shown only once):"
  echo "    $DRIFT_USER_PASSWORD_GENERATED"
  echo "  Use this to log in via the Drift web terminal. Rotate with:"
  echo "    passwd drift  (run on this device as root)"
  echo "===================================================================="
fi
