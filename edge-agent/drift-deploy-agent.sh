#!/usr/bin/env bash
# Drift Deploy edge agent — v0 (bash + systemd).
#
# Loaded from /etc/drift-deploy/env:
#   DEVICE_NAME, BOOTSTRAP_TOKEN, CP_URL, POLL_INTERVAL (default 30s).
#   GROUP_ID (optional) — surfaced to compose as DRIFT_GROUP_ID.
#
# Auth model: bearer-only. The Caddy reverse proxy is configured to NOT
# basic_auth /drift/api/deploy/agent/* paths because we can't send both
# Caddy's `Authorization: Basic` and our `Authorization: Bearer` in the
# same request (HTTP Authorization is single-valued).
#
# Per-device facts exposed to bundles via compose env-var interpolation:
#   DRIFT_DEVICE_NAME   — this device's name in the control plane
#   DRIFT_GROUP_ID      — logical grouping (cloud/edge/client/...) or ""
# A compose file can reference these as ${DRIFT_DEVICE_NAME} etc. to make
# one bundle deployable across heterogeneous devices.
#
# Safety: PROTECTED_NAMES below is a hard-coded blocklist of service /
# container names that this agent will REFUSE to deploy under any
# circumstance — bricking protection. The agent itself, its DB, the
# frontend, etc. must never be redeployed by this script.
#
# Loop: every POLL_INTERVAL seconds, POST /agent/check-in with current
# revisions, receive desired state with presigned bundle URLs, apply any
# drift via `docker compose up -d` from the extracted bundle directory.
#
# Safety rails (v0):
#   - MANAGED_APPS is a strict allowlist; unknown apps are skipped with a warning.
#   - sha256 verified before extraction; mismatch aborts.
#   - 30s post-`up` health check (all services must report `running`).
#   - flock prevents overlapping runs.

set -euo pipefail

: "${DEVICE_NAME:?DEVICE_NAME required}"
: "${BOOTSTRAP_TOKEN:?BOOTSTRAP_TOKEN required}"
: "${CP_URL:?CP_URL required}"
: "${GROUP_ID:?GROUP_ID required (set in /etc/drift-deploy/env)}"
: "${POLL_INTERVAL:=30}"
: "${DRIFT_DOCKER_DATA_DIR:=/var/lib/docker}"

# ---- Self-update bootstrap ----
# Runs once at container start (and on demand mid-loop after the agent
# `exit 0`s for self-update). Fetches the latest agent.sh from the
# control plane, validates with `bash -n`, execs into it if it differs
# from this script. Network/parse failures fall through to the in-image
# baseline; the image's baked-in script is the last-known-good fallback.
#
# Guarded by DRIFT_SKIP_BOOTSTRAP=1 to prevent infinite exec loops if
# the new script keeps re-running this block.
if [ -z "${DRIFT_SKIP_BOOTSTRAP:-}" ]; then
  _DRIFT_NEW_SH=$(mktemp /tmp/drift-deploy-agent.XXXXXX.sh)
  if curl -sS -H "Authorization: Bearer $BOOTSTRAP_TOKEN" \
          --connect-timeout 5 --max-time 20 \
          "$CP_URL/agent/agent.sh" -o "$_DRIFT_NEW_SH" 2>/dev/null \
     && [ -s "$_DRIFT_NEW_SH" ] \
     && bash -n "$_DRIFT_NEW_SH" 2>/dev/null; then
    _MY_SHA=$(sha256sum "$0" 2>/dev/null | cut -c1-12)
    _NEW_SHA=$(sha256sum "$_DRIFT_NEW_SH" | cut -c1-12)
    if [ "$_MY_SHA" != "$_NEW_SHA" ]; then
      chmod +x "$_DRIFT_NEW_SH"
      # Set the skip flag so the re-execed script doesn't re-bootstrap.
      # Persist the script at a stable path so the AGENT_SHA computed
      # inside matches what the control plane expects.
      cp "$_DRIFT_NEW_SH" /usr/local/bin/drift-deploy-agent.sh
      rm -f "$_DRIFT_NEW_SH"
      DRIFT_SKIP_BOOTSTRAP=1 exec /usr/local/bin/drift-deploy-agent.sh
    fi
  fi
  rm -f "$_DRIFT_NEW_SH"
fi

# Per-device facts the agent exports into every `docker compose` subshell.
# Bundles reference these via ${DRIFT_DEVICE_NAME}, ${DRIFT_GROUP_ID},
# ${DRIFT_DOCKER_DATA_DIR}.
DRIFT_DEVICE_NAME="$DEVICE_NAME"
DRIFT_GROUP_ID="$GROUP_ID"
# DRIFT_DOCKER_DATA_DIR is already in env; just make it explicit.
# DRIFT_HOST_CA_BUNDLE is populated by install.sh when the host has a
# combined CA bundle (Debian/Ubuntu/RHEL/Alpine). Empty on hosts where
# none was detected — the override generator gates injection on it.
DRIFT_HOST_CA_BUNDLE="${DRIFT_HOST_CA_BUNDLE:-}"

# CP-side facts (public URL, vmauth write credentials) — populated on
# each check-in (see reconcile_once). Persisted to /etc/drift-deploy/cp-env
# so values survive across the flock'd reconcile subshells. Bundles can
# reference these as ${DRIFT_CP_PUBLIC_URL}, ${DRIFT_VM_WRITE_USER},
# ${DRIFT_VM_WRITE_PASSWORD} — e.g. a vmagent's `-remoteWrite.url=
# ${DRIFT_CP_PUBLIC_URL}/vm/api/v1/write`.
CP_ENV_FILE=/etc/drift-deploy/cp-env
if [ -f "$CP_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CP_ENV_FILE"
fi
DRIFT_CP_PUBLIC_URL="${DRIFT_CP_PUBLIC_URL:-}"
DRIFT_VM_WRITE_USER="${DRIFT_VM_WRITE_USER:-}"
DRIFT_VM_WRITE_PASSWORD="${DRIFT_VM_WRITE_PASSWORD:-}"

# Atomic-write the cp-env file. Values bash-quote-escape '"' and '\'
# defensively — current rand_token output doesn't contain either, but
# operator-set values might.
write_cp_env() {
  local cp_url=$1 vm_user=$2 vm_pw=$3
  local tmp
  tmp=$(mktemp -p /etc/drift-deploy cp-env.XXXXXX 2>/dev/null) || return 0
  {
    printf 'DRIFT_CP_PUBLIC_URL=%q\n' "$cp_url"
    printf 'DRIFT_VM_WRITE_USER=%q\n' "$vm_user"
    printf 'DRIFT_VM_WRITE_PASSWORD=%q\n' "$vm_pw"
  } > "$tmp" && chmod 600 "$tmp" && mv "$tmp" "$CP_ENV_FILE" || rm -f "$tmp"
}

# Bricking protection. Bundles whose compose file declares any of these
# as a service name OR via container_name: are rejected with apply_error.
# Extend if you add critical infra that should never be self-redeployed.
PROTECTED_NAMES=(drift-agent drift-postgres drift-frontend drift-deploy-agent)

STATE_DIR=/var/lib/drift-deploy
APPS_DIR="$STATE_DIR/apps"
STATE_FILE="$STATE_DIR/state.json"
LOCK_FILE="$STATE_DIR/agent.lock"
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}
TEXTFILE_PATH="$TEXTFILE_DIR/drift_deploy_agent.prom"
# Ensure the textfile dir exists with perms node-exporter (uid 65534
# nobody) can traverse + read. install.sh creates this on first
# commission but Synology DSM (and some other hosts) inherit a
# restrictive default umask that leaves the dir at 700 — node-exporter
# then errors "open /var/lib/node_exporter/textfile_collector:
# permission denied" on every scrape, losing all node metrics. We're
# running inside a bind-mount to the host path so chmod here adjusts
# the host inode. Idempotent + cheap; runs once per agent start.
mkdir -p "$TEXTFILE_DIR" 2>/dev/null || true
chmod 755 "$TEXTFILE_DIR" 2>/dev/null || true
# Bump on every script change. The check-in payload + textfile metric
# both report this so the control plane can tell at-a-glance which
# devices are running which agent. Companion sha256 (12 chars) computed
# at startup so even if the version is forgotten, the running code can
# always be identified.
AGENT_VERSION="0.12.0"
AGENT_SHA="$(sha256sum "$0" 2>/dev/null | cut -c1-12 || echo unknown)"
LOCK_ACQUIRED_AT="$STATE_DIR/.lock-acquired-at"

# Host fingerprint sent on every check-in. The CP TOFUs it on the
# first arrival after commissioning and rejects mismatches afterwards
# (returns 409). Stops accidental cross-host paste of the
# commissioning curl from silently flipping device identity between
# two machines. Computed once at startup; the value is stable for the
# life of the host's OS install.
#
# Sources, in fallback order:
#   1. /host/etc/machine-id        (systemd; standard on Linux)
#   2. /host/var/lib/dbus/machine-id (older spec)
#   3. /host/sys/class/dmi/id/product_uuid (hardware DMI; root-readable)
#
# install.sh bind-mounts each of these under /host/* when they exist
# on the host. If none is available we send empty — the CP doesn't
# enforce on absent fingerprint, since embedded distros (some
# BusyBox setups) don't ship any of these.
HOST_FINGERPRINT=""
for _fp_src in \
    /host/etc/machine-id \
    /host/var/lib/dbus/machine-id \
    /host/sys/class/dmi/id/product_uuid; do
  if [ -r "$_fp_src" ]; then
    _fp_val=$(tr -d '[:space:]' < "$_fp_src" 2>/dev/null || true)
    if [ -n "$_fp_val" ]; then
      HOST_FINGERPRINT=$(printf '%s' "$_fp_val" | sha256sum | cut -c1-64)
      break
    fi
  fi
done
unset _fp_src _fp_val
if [ -z "$HOST_FINGERPRINT" ]; then
  echo "warn: no host fingerprint source available (/etc/machine-id absent or unreadable); CP will accept this device without TOFU enforcement" >&2
fi

FRESH_STATE='{"current_revisions": {}, "apply_errors": {}, "metrics": {"check_in_ok": 0, "check_in_error": 0, "apply_ok": 0, "apply_error": 0}}'

mkdir -p "$APPS_DIR" "$TEXTFILE_DIR"

# If state.json doesn't exist OR has zero bytes (a previous-agent
# cross-fs mv truncated it), start fresh.
if [ ! -s "$STATE_FILE" ]; then
  printf '%s\n' "$FRESH_STATE" > "$STATE_FILE"
fi

# Migrate state.json shapes from older agents (pre-0.2.3 didn't always
# write apply_errors). Idempotent — only rewrites if the key is missing.
# Tmp file is colocated with state.json so the eventual rename is atomic
# same-fs. Also size-check the tmp before mv so a silent empty-output
# jq pass can't clobber what we already have.
if ! jq -e '.apply_errors' "$STATE_FILE" >/dev/null 2>&1; then
  _migrate_tmp=$(mktemp -p "$STATE_DIR" .state.migrate.XXXXXX)
  if jq '. + {apply_errors: (.apply_errors // {})}' "$STATE_FILE" > "$_migrate_tmp" 2>/dev/null \
     && [ -s "$_migrate_tmp" ]; then
    mv "$_migrate_tmp" "$STATE_FILE"
  else
    rm -f "$_migrate_tmp"
    # state.json is unreadable AND we couldn't migrate; reset.
    printf '%s\n' "$FRESH_STATE" > "$STATE_FILE"
  fi
fi

# Log helpers. Each level includes a word Vector's classifier regex
# matches (error, warn, info) so log_to_metric produces correct
# container_log_lines_total{level=...} counters out of the box.
log()       { printf '[%s] INFO  %s\n'  "$(date -u +%H:%M:%S)" "$*"; }
log_info()  { log "$*"; }
log_warn()  { printf '[%s] WARN  %s\n'  "$(date -u +%H:%M:%S)" "$*" >&2; }
log_error() { printf '[%s] ERROR %s\n'  "$(date -u +%H:%M:%S)" "$*" >&2; }

# Atomic-write helper. Creates the tmp file in the SAME directory as the
# target so the eventual `mv` is a rename(2), not cross-fs copy+unlink.
# Cross-fs mv is non-atomic and can leave the destination empty if the
# copy fails mid-stream — that's how Pi devices with slow SD cards ended
# up with a 0-byte state.json.
atomic_jq() {
  local target=$1; shift
  local dir tmp
  dir=$(dirname "$target")
  tmp=$(mktemp -p "$dir" ".$(basename "$target").XXXXXX")
  # `jq EXPR EMPTY_FILE` exits 0 with empty output — without the size
  # check, we'd happily clobber state.json with zero bytes the moment
  # it became invalid even once. Treat empty output as failure so we
  # leave the existing target alone and let the migration / next call
  # decide what to do.
  if jq "$@" "$target" > "$tmp" && [ -s "$tmp" ]; then
    mv "$tmp" "$target"
    return 0
  else
    rm -f "$tmp"
    return 1
  fi
}

generate_drift_override() {
  # Write a compose.override.yaml that injects Drift identity into every
  # service in the bundle. Docker compose auto-merges override files in the
  # same directory. Adds env vars (visible to the app) AND container labels
  # (visible at the Docker / cAdvisor layer for fleet-wide queries like
  # `container_last_seen{container_label_drift_managed="true"}`).
  local rev_dir=$1 app=$2 rev_id=$3
  local override="$rev_dir/compose.override.yaml"
  local services
  services=$(cd "$rev_dir" \
    && DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" \
       DRIFT_APP="$app" DRIFT_DOCKER_DATA_DIR="$DRIFT_DOCKER_DATA_DIR" \
       DRIFT_HOST_CA_BUNDLE="$DRIFT_HOST_CA_BUNDLE" DRIFT_CP_PUBLIC_URL="$DRIFT_CP_PUBLIC_URL" DRIFT_VM_WRITE_USER="$DRIFT_VM_WRITE_USER" DRIFT_VM_WRITE_PASSWORD="$DRIFT_VM_WRITE_PASSWORD" \
       docker compose config --services 2>/dev/null)
  if [ -z "$services" ]; then
    log_warn "[$app] could not enumerate services for override; bundle env-vars will still apply via shell"
    return 0
  fi
  {
    printf '# Drift Deploy injected identity. Auto-generated; do not edit.\n'
    printf 'services:\n'
    while IFS= read -r svc; do
      [ -z "$svc" ] && continue
      printf '  %s:\n' "$svc"
      printf '    environment:\n'
      printf '      DRIFT_DEVICE_NAME: "%s"\n' "$DRIFT_DEVICE_NAME"
      printf '      DRIFT_GROUP_ID: "%s"\n' "$DRIFT_GROUP_ID"
      printf '      DRIFT_APP: "%s"\n' "$app"
      # Operator-PKI / corp TLS-intercepting proxy trust. SSL_CERT_FILE
      # and CURL_CA_BUNDLE are honored by curl, openssl, Go's crypto/tls,
      # Python's ssl module, and most cert-aware tooling — so apps trust
      # the host's CA roots regardless of the image's distro. The volumes
      # block below also overrides the standard Debian/Alpine bundle
      # paths for tools that read those directly.
      if [ -n "$DRIFT_HOST_CA_BUNDLE" ]; then
        printf '      SSL_CERT_FILE: "/etc/ssl/certs/ca-certificates.crt"\n'
        printf '      CURL_CA_BUNDLE: "/etc/ssl/certs/ca-certificates.crt"\n'
      fi
      printf '    labels:\n'
      printf '      drift.managed: "true"\n'
      printf '      drift.device_name: "%s"\n' "$DRIFT_DEVICE_NAME"
      printf '      drift.group_id: "%s"\n' "$DRIFT_GROUP_ID"
      printf '      drift.app: "%s"\n' "$app"
      printf '      drift.revision: "%s"\n' "$rev_id"
      # Bind-mount the host's combined CA bundle into BOTH the
      # Debian/Ubuntu standard path and the Alpine/BSD standard path so
      # apps that read either one trust the operator's PKI. If a bundle
      # already declares a volume targeting one of these paths, compose
      # will refuse to start the service ("duplicate mount point") —
      # resolve by removing the bundle's redundant mount.
      if [ -n "$DRIFT_HOST_CA_BUNDLE" ]; then
        printf '    volumes:\n'
        printf '      - "%s:/etc/ssl/certs/ca-certificates.crt:ro"\n' "$DRIFT_HOST_CA_BUNDLE"
        printf '      - "%s:/etc/ssl/cert.pem:ro"\n' "$DRIFT_HOST_CA_BUNDLE"
      fi
    done <<< "$services"
  } > "$override"
}


bundle_touches_protected() {
  # Returns 0 (true) if the bundle's compose declares a service name or a
  # container_name that matches PROTECTED_NAMES. Uses `docker compose config`
  # so we get real YAML parsing — no fragile grepping.
  local rev_dir=$1
  local names
  names=$(cd "$rev_dir" && \
    DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" \
    DRIFT_APP="" DRIFT_DOCKER_DATA_DIR="$DRIFT_DOCKER_DATA_DIR" \
    DRIFT_HOST_CA_BUNDLE="$DRIFT_HOST_CA_BUNDLE" DRIFT_CP_PUBLIC_URL="$DRIFT_CP_PUBLIC_URL" DRIFT_VM_WRITE_USER="$DRIFT_VM_WRITE_USER" DRIFT_VM_WRITE_PASSWORD="$DRIFT_VM_WRITE_PASSWORD" \
    docker compose config --format json 2>/dev/null \
    | jq -r '.services | to_entries[] | (.key, .value.container_name // empty)' \
    | sort -u)
  if [ -z "$names" ]; then
    return 1  # can't parse — let docker compose itself surface the error later
  fi
  local p
  for p in "${PROTECTED_NAMES[@]}"; do
    if printf '%s\n' "$names" | grep -Fxq "$p"; then
      log_warn "blocklist hit: '$p' appears in compose as service or container_name"
      return 0
    fi
  done
  return 1
}

curl_cp() {
  # Aggressive timeouts so a single tick can't run longer than the poll
  # interval: connect within 5s, total within 25s. Anything beyond is
  # genuine network distress.
  curl -sS -H "Authorization: Bearer $BOOTSTRAP_TOKEN" \
       -H "Content-Type: application/json" \
       --connect-timeout 5 --max-time 25 "$@"
}

state_set_current() {
  local app=$1 rev=$2
  atomic_jq "$STATE_FILE" \
    --arg a "$app" --arg r "$rev" \
    '.apply_errors //= {} | del(.apply_errors[$a]) | .current_revisions[$a] = $r' \
    || log_warn "state_set_current jq failed for app=$app"
}

state_set_error() {
  local app=$1
  # Trim to 500 chars so an exploding stack trace doesn't bloat the check-in body.
  local err="${2:0:500}"
  atomic_jq "$STATE_FILE" \
    --arg a "$app" --arg e "$err" \
    '.apply_errors //= {} | .apply_errors[$a] = $e' \
    || log_warn "state_set_error jq failed for app=$app"
}

state_inc() {
  local key=$1
  atomic_jq "$STATE_FILE" \
    --arg k "$key" \
    '.metrics[$k] = ((.metrics[$k] // 0) + 1)' \
    || log_warn "state_inc jq failed for key=$key"
}

write_textfile() {
  # Atomic textfile write for node-exporter's textfile collector. Tmp
  # in the SAME dir so the eventual mv is rename(2), not cross-fs.
  local tmp now ok_ci err_ci ok_ap err_ap current_lines=""
  tmp=$(mktemp -p "$TEXTFILE_DIR" ".drift_deploy_agent.XXXXXX") || return
  now=$(date +%s)
  ok_ci=$(jq -r '.metrics.check_in_ok // 0' "$STATE_FILE")
  err_ci=$(jq -r '.metrics.check_in_error // 0' "$STATE_FILE")
  ok_ap=$(jq -r '.metrics.apply_ok // 0' "$STATE_FILE")
  err_ap=$(jq -r '.metrics.apply_error // 0' "$STATE_FILE")

  {
    printf '# HELP drift_deploy_agent_info Agent version + identity (constant 1).\n'
    printf '# TYPE drift_deploy_agent_info gauge\n'
    printf 'drift_deploy_agent_info{device="%s",group_id="%s",version="%s",sha="%s"} 1\n' \
      "$DEVICE_NAME" "$GROUP_ID" "$AGENT_VERSION" "$AGENT_SHA"

    printf '# HELP drift_deploy_agent_last_check_in_timestamp_seconds Unix epoch of last successful check-in.\n'
    printf '# TYPE drift_deploy_agent_last_check_in_timestamp_seconds gauge\n'
    printf 'drift_deploy_agent_last_check_in_timestamp_seconds %s\n' "$now"

    printf '# HELP drift_deploy_agent_check_ins_total Check-in attempts by result.\n'
    printf '# TYPE drift_deploy_agent_check_ins_total counter\n'
    printf 'drift_deploy_agent_check_ins_total{result="ok"} %s\n' "$ok_ci"
    printf 'drift_deploy_agent_check_ins_total{result="error"} %s\n' "$err_ci"

    printf '# HELP drift_deploy_agent_applies_total Apply (compose up) attempts by result.\n'
    printf '# TYPE drift_deploy_agent_applies_total counter\n'
    printf 'drift_deploy_agent_applies_total{result="ok"} %s\n' "$ok_ap"
    printf 'drift_deploy_agent_applies_total{result="error"} %s\n' "$err_ap"

    # One line per (app, revision) the device currently runs.
    printf '# HELP drift_deploy_agent_current_revision Current revision id per app (constant 1).\n'
    printf '# TYPE drift_deploy_agent_current_revision gauge\n'
    # `// {}` guards against state.json being malformed or missing the
    # key. Without this guard a `null | to_entries[]` crashes the
    # script under set -euo pipefail, which puts the container into a
    # restart spiral.
    jq -r '(.current_revisions // {}) | to_entries[] | "drift_deploy_agent_current_revision{app=\"\(.key)\",revision=\"\(.value)\"} 1"' "$STATE_FILE" 2>/dev/null || true
  } > "$tmp"
  # node-exporter's textfile collector runs as uid 65534 (nobody) and
  # can't read a default-umask 644 file owned by root — wait, it can.
  # The real problem is umask 077 in the container (mktemp inherits it
  # if we ever tighten the umask), so make the chmod explicit. 644 is
  # safe: this file is per-host self-metrics, no secrets.
  chmod 644 "$tmp"
  mv "$tmp" "$TEXTFILE_PATH"
}

apply_revision() {
  local app=$1 rev_id=$2 url=$3 sha=$4
  local rev_dir="$APPS_DIR/$app/$rev_id"
  local bundle_gz="$rev_dir/bundle.tar.gz"
  local bundle_tar="$rev_dir/bundle.tar"
  local err   # last error message, captured for state_set_error

  log_info "[$app] applying revision $rev_id"
  mkdir -p "$rev_dir"

  # CP-served bundles arrive as `local:<filename>` — prepend ${CP_URL}
  # and add the bearer token. Presigned S3 URLs come through verbatim
  # and need no auth header.
  local curl_args=(-fsSL --max-time 120 -o "$bundle_gz")
  if [[ "$url" == local:* ]]; then
    local fname="${url#local:}"
    url="${CP_URL%/}/agent/bundles/$fname"
    curl_args+=(-H "Authorization: Bearer $BOOTSTRAP_TOKEN")
  fi

  if ! err=$(curl "${curl_args[@]}" "$url" 2>&1); then
    log_error "[$app] bundle download failed: $err"
    state_set_error "$app" "bundle download failed: $err"
    return 1
  fi

  # Verify sha256 of the UNCOMPRESSED tar (not the gzip). gzip's
  # output isn't byte-stable across implementations / dates, so
  # verifying the inner tar gives us a deterministic fingerprint.
  if ! err=$(gunzip -c "$bundle_gz" > "$bundle_tar" 2>&1); then
    log_error "[$app] bundle gunzip failed: $err"
    state_set_error "$app" "bundle gunzip failed: $err"
    return 1
  fi
  local got
  got=$(sha256sum "$bundle_tar" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then
    err="sha256 mismatch (got $got, expected $sha)"
    log_error "[$app] $err"
    state_set_error "$app" "$err"
    return 1
  fi
  if ! err=$(tar -xf "$bundle_tar" -C "$rev_dir" 2>&1); then
    log_error "[$app] extract failed: $err"
    state_set_error "$app" "extract failed: $err"
    return 1
  fi
  # Don't keep the uncompressed tar around — only the .gz needs to
  # stick around for redeploys after a CP outage.
  rm -f "$bundle_tar"

  if bundle_touches_protected "$rev_dir"; then
    err="REFUSED: bundle touches a protected service/container name — bricking safeguard"
    log_warn "[$app] $err"
    state_set_error "$app" "$err"
    return 1
  fi

  # Conflict pre-flight (Layer B). Parse compose for explicit
  # container_name: declarations; if any name is already in use by a
  # container we don't own (i.e. drift.app label is missing or refers
  # to a different app), report a clean apply_error instead of letting
  # docker fail with its own less-readable "name in use" message.
  # Names we DO own (drift.app == $app) are fine — compose up will
  # recreate them in place.
  local compose_file
  for cand in compose.yaml compose.yml docker-compose.yml; do
    if [ -f "$rev_dir/$cand" ]; then compose_file="$rev_dir/$cand"; break; fi
  done
  if [ -n "${compose_file:-}" ]; then
    local declared_names
    declared_names=$(awk '/^[[:space:]]*container_name:[[:space:]]*/ {
      gsub(/^[[:space:]]*container_name:[[:space:]]*/, "", $0);
      gsub(/[[:space:]]*#.*$/, "", $0);
      gsub(/["'"'"']/, "", $0);
      gsub(/[[:space:]]+$/, "", $0);
      if ($0 != "") print $0;
    }' "$compose_file")
    local conflicts=""
    if [ -n "$declared_names" ]; then
      while IFS= read -r name; do
        [ -z "$name" ] && continue
        # Container with this exact name?
        local cid owner
        cid=$(docker ps -a --filter "name=^${name}$" --format '{{.ID}}' 2>/dev/null | head -1)
        if [ -n "$cid" ]; then
          owner=$(docker inspect -f '{{ index .Config.Labels "drift.app" }}' "$cid" 2>/dev/null)
          if [ -z "$owner" ]; then
            conflicts="$conflicts $name(unmanaged)"
          elif [ "$owner" != "$app" ]; then
            conflicts="$conflicts $name(owned-by:$owner)"
          fi
        fi
      done <<< "$declared_names"
    fi
    if [ -n "$conflicts" ]; then
      err="REFUSED: container_name conflicts —$conflicts. Remove the listed containers manually (or via Drift's app removal) and retry."
      log_warn "[$app] $err"
      state_set_error "$app" "$err"
      return 1
    fi
  fi

  generate_drift_override "$rev_dir" "$app" "$rev_id"

  # -p <app-name> pins the compose project name to the app, so containers
  # get human-readable names (hello-world-echo-1) instead of UUID-prefixed
  # ones derived from the per-revision parent directory.
  # DRIFT_* facts are exported so the generated compose.override.yaml
  # AND the bundle's compose can interpolate them.
  if ! err=$( cd "$rev_dir" \
       && export DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" \
                 DRIFT_APP="$app" DRIFT_DOCKER_DATA_DIR="$DRIFT_DOCKER_DATA_DIR" \
                 DRIFT_HOST_CA_BUNDLE="$DRIFT_HOST_CA_BUNDLE" DRIFT_CP_PUBLIC_URL="$DRIFT_CP_PUBLIC_URL" DRIFT_VM_WRITE_USER="$DRIFT_VM_WRITE_USER" DRIFT_VM_WRITE_PASSWORD="$DRIFT_VM_WRITE_PASSWORD" \
       && docker compose -p "$app" pull 2>&1 \
       && docker compose -p "$app" up -d --remove-orphans 2>&1 ); then
    # err contains the combined output; take the last few lines for the
    # control plane, full text stays in journald/docker logs.
    local err_tail
    err_tail=$(printf '%s' "$err" | tail -n 5 | tr '\n' ' ')
    log_error "[$app] docker compose up failed: $err_tail"
    state_set_error "$app" "compose up failed: $err_tail"
    return 1
  fi

  sleep 30
  local bad
  bad=$( cd "$rev_dir" \
       && export DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" \
                 DRIFT_APP="$app" DRIFT_DOCKER_DATA_DIR="$DRIFT_DOCKER_DATA_DIR" \
                 DRIFT_HOST_CA_BUNDLE="$DRIFT_HOST_CA_BUNDLE" DRIFT_CP_PUBLIC_URL="$DRIFT_CP_PUBLIC_URL" DRIFT_VM_WRITE_USER="$DRIFT_VM_WRITE_USER" DRIFT_VM_WRITE_PASSWORD="$DRIFT_VM_WRITE_PASSWORD" \
       && docker compose -p "$app" ps --format json \
       | jq -c 'select(.State != "running") | {Service, State}' )
  if [ -n "$bad" ]; then
    log_error "[$app] post-up health check failed: $bad"
    state_set_error "$app" "post-up health check failed: $bad"
    return 1
  fi

  state_set_current "$app" "$rev_id"
  log_info "[$app] healthy at revision $rev_id"
  state_inc apply_ok
}

remove_app() {
  local app=$1
  # Same blocklist guard as apply — refuse to compose-down a protected
  # service name even if the control plane told us to.
  local p
  for p in "${PROTECTED_NAMES[@]}"; do
    if [ "$app" = "$p" ]; then
      log_warn "[$app] REFUSED to remove: matches blocklist (bricking safeguard)"
      state_set_error "$app" "REFUSED: matches blocklist"
      return 1
    fi
  done

  log_info "[$app] removing (docker compose -p $app down --remove-orphans)"
  # `docker compose -p <project> down` works against the daemon's known
  # project state — doesn't need a compose file. If the project isn't
  # running (already torn down on this device), exit code is still 0.
  if ! docker compose -p "$app" down --remove-orphans >/dev/null 2>&1; then
    log_warn "[$app] docker compose down had issues; clearing local state anyway"
  fi

  # Defensive cleanup: sweep any container still labeled drift.app=$app.
  # Belt-and-suspenders for the rare race where `compose down` returns 0
  # before the daemon has fully reaped a container (especially likely
  # when the compose declared an explicit container_name: that overrode
  # the project-prefix scheme). Restricted to the drift.app label, which
  # generate_drift_override stamps on every Drift-created container —
  # so this only touches Drift's own state and never user-managed
  # containers that might happen to share a name.
  local stale
  stale=$(docker ps -a --filter "label=drift.app=$app" -q 2>/dev/null)
  if [ -n "$stale" ]; then
    log_info "[$app] removing $(echo "$stale" | wc -l | tr -d ' ') stale container(s) with drift.app=$app"
    echo "$stale" | xargs -r docker rm -f >/dev/null 2>&1 \
      || log_warn "[$app] stale-container cleanup hit errors (continuing)"
  fi

  # Drop the app from current_revisions + clear any pending error.
  atomic_jq "$STATE_FILE" --arg a "$app" \
    'del(.current_revisions[$a]) | (.apply_errors //= {}) | del(.apply_errors[$a])' \
    || log_warn "remove_app: state update failed for $app"
  log_info "[$app] removed"
  state_inc apply_ok
}


# Identity facts about the host — interfaces, hostname, arch, os,
# kernel, docker_version. Collected periodically (FACTS_EVERY_N_TICKS),
# overwritten on the CP each time. For operational time-series (disk,
# mem, uptime, CPU) use node-exporter via reporter — these only need
# slow-changing identity bits.
FACTS_EVERY_N_TICKS=${FACTS_EVERY_N_TICKS:-20}    # ≈10min at 30s poll
_FACTS_TICK_COUNTER=0
_FACTS_CACHED='{}'

collect_facts() {
  # Interfaces → {ifname: [ip, ip, ...]}. Parse `ip addr show` text
  # output rather than `ip -j` so this works on busybox's stripped-down
  # `ip` (the agent's alpine image ships busybox, not full iproute2).
  # Pure awk: pick "scope global" inet lines, group by ifname, emit JSON.
  local interfaces
  interfaces=$(ip addr show 2>/dev/null | awk '
    /^[0-9]+: / { gsub(/[:@].*$/, "", $2); iface = $2 }
    /^[[:space:]]+inet / && /scope global/ {
      ip = $2; sub("/.*", "", ip)
      ifaces[iface] = ifaces[iface] (ifaces[iface] ? "," : "") ip
    }
    END {
      printf "{"
      first = 1
      for (k in ifaces) {
        if (!first) printf ","
        first = 0
        n = split(ifaces[k], arr, ",")
        printf "\"%s\":[", k
        for (i = 1; i <= n; i++) {
          if (i > 1) printf ","
          printf "\"%s\"", arr[i]
        }
        printf "]"
      }
      printf "}"
    }
  ' 2>/dev/null)
  # Belt-and-suspenders: if the awk pipeline produced nothing for any
  # reason, fall back to an empty object. jq below will accept it.
  [ -z "$interfaces" ] && interfaces='{}'

  local host arch kernel os docker_v
  # /host/etc/hostname is bind-mounted from the host in install.sh; the
  # in-container `hostname` returns the container ID, which is useless.
  # Fall back to in-container hostname if the bind mount is missing
  # (older installs that haven't re-run install.sh yet).
  if [ -r /host/etc/hostname ]; then
    host=$(cat /host/etc/hostname 2>/dev/null | tr -d '[:space:]')
  fi
  host=${host:-$(hostname 2>/dev/null || echo unknown)}
  # uname reflects the host kernel since containers share it — these
  # two are correct in-container without any bind mount.
  arch=$(uname -m 2>/dev/null || echo unknown)
  kernel=$(uname -r 2>/dev/null || echo unknown)
  # Host OS detection — try a sequence of host-bind-mounted source
  # files, first-hit wins. install.sh decides which ones to mount
  # based on what's actually present on the host:
  #   /host/etc/os-release         — systemd-era standard (~95% of distros)
  #   /host/usr/lib/os-release     — systemd fallback (rare cases where /etc is missing)
  #   /host/etc.defaults/VERSION   — Synology DSM
  #   /host/etc/lsb-release        — older Ubuntu / Debian derivatives
  # If none match, fall through to the in-container /etc/os-release
  # (alpine) and ultimately "unknown".
  os=""
  for src in /host/etc/os-release /host/usr/lib/os-release; do
    if [ -z "$os" ] && [ -r "$src" ]; then
      os=$(. "$src" 2>/dev/null && echo "${PRETTY_NAME:-${NAME:-}}")
    fi
  done
  # Synology DSM has /etc.defaults/VERSION in shell-source-able format:
  # productversion="7.2.1", buildnumber="69057", etc. Wrap as DSM <ver>.
  if [ -z "$os" ] && [ -r /host/etc.defaults/VERSION ]; then
    os=$(. /host/etc.defaults/VERSION 2>/dev/null \
          && [ -n "${productversion:-}" ] \
          && echo "DSM ${productversion}${buildnumber:+ (build $buildnumber)}")
  fi
  # /etc/lsb-release uses DISTRIB_DESCRIPTION (or compose from
  # DISTRIB_ID + DISTRIB_RELEASE). Same source-able shell-vars shape.
  if [ -z "$os" ] && [ -r /host/etc/lsb-release ]; then
    os=$(. /host/etc/lsb-release 2>/dev/null \
          && echo "${DISTRIB_DESCRIPTION:-${DISTRIB_ID}${DISTRIB_RELEASE:+ $DISTRIB_RELEASE}}")
  fi
  # Last resort: in-container /etc/os-release (alpine baseline).
  if [ -z "$os" ] && [ -r /etc/os-release ]; then
    os=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-${NAME:-unknown}}")
  fi
  os=${os:-unknown}
  docker_v=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)

  jq -n \
    --argjson interfaces "$interfaces" \
    --arg hostname "$host" \
    --arg arch "$arch" \
    --arg kernel "$kernel" \
    --arg os "$os" \
    --arg docker_version "$docker_v" \
    '{interfaces:$interfaces, hostname:$hostname, arch:$arch, kernel:$kernel, os:$os, docker_version:$docker_version}'
}


reconcile_once() {
  local current errors
  current=$(jq -c '.current_revisions // {}' "$STATE_FILE" 2>/dev/null)
  errors=$(jq -c '.apply_errors // {}' "$STATE_FILE" 2>/dev/null)
  # Belt-and-suspenders: a corrupted state.json from a pre-migration
  # agent could leave these empty. Default to '{}' so --argjson is happy.
  current=${current:-'{}'}
  errors=${errors:-'{}'}

  # Recompute identity facts every FACTS_EVERY_N_TICKS (default 20 ≈
  # 10min at 30s poll). Tick 0 always collects so the CP gets the
  # facts on the agent's first check-in after start/upgrade. Send
  # them ONLY on collection ticks so we don't waste bytes 19 times
  # out of 20 (the CP keeps the prior snapshot when facts is absent).
  # Fact collection is wrapped in `|| true` so a malformed `ip addr`
  # output or any other glitch can never crash the main loop (which
  # would put us in a docker restart spiral under set -euo pipefail).
  local facts_field=""
  if [ "$_FACTS_TICK_COUNTER" -eq 0 ]; then
    set +e
    _FACTS_CACHED=$(collect_facts 2>/dev/null)
    if [ -z "$_FACTS_CACHED" ]; then _FACTS_CACHED='{}'; fi
    set -e
    facts_field=", facts: \$f"
  fi
  _FACTS_TICK_COUNTER=$(( (_FACTS_TICK_COUNTER + 1) % FACTS_EVERY_N_TICKS ))

  local body resp
  # host_fingerprint goes on every check-in (cheap, 64 bytes). The CP
  # TOFUs on first arrival and rejects mismatches with 409 — see
  # routes_agent.py:check_in. Empty string when no fingerprint source
  # was readable at startup; the CP treats that as "not provided" and
  # doesn't enforce.
  if [ -n "$facts_field" ]; then
    body=$(jq -n --arg n "$DEVICE_NAME" --arg v "$AGENT_VERSION" --arg g "$GROUP_ID" \
                --arg fp "$HOST_FINGERPRINT" \
                --argjson c "$current" --argjson e "$errors" --argjson f "$_FACTS_CACHED" \
      '{device_name:$n, agent_version:$v, group_id:$g, host_fingerprint:$fp, current_revisions:$c, apply_errors:$e, health:{}, facts:$f}')
  else
    body=$(jq -n --arg n "$DEVICE_NAME" --arg v "$AGENT_VERSION" --arg g "$GROUP_ID" \
                --arg fp "$HOST_FINGERPRINT" \
                --argjson c "$current" --argjson e "$errors" \
      '{device_name:$n, agent_version:$v, group_id:$g, host_fingerprint:$fp, current_revisions:$c, apply_errors:$e, health:{}}')
  fi

  # Check-in failure modes the operator should be able to distinguish
  # from the logs without docker exec'ing in:
  #   - network: DNS failure, host unreachable, TCP refused, TLS error
  #   - 401/403: bootstrap token mismatch (commonly: device was deleted
  #     and re-commissioned, but /etc/drift-deploy/env still has the
  #     old token)
  #   - 5xx: CP itself is unhealthy (deployment, postgres dead, etc.)
  #   - non-JSON 200: something upstream is intercepting (Caddy returning
  #     a basic_auth challenge, nginx returning HTML for some other path)
  # We capture status code + body separately so each gets a clean log
  # line and the response is never piped to jq blindly.
  local _body_file _http_code _curl_rc
  _body_file=$(mktemp /tmp/drift-checkin-resp.XXXXXX)
  _http_code=$(curl -sS -o "$_body_file" -w '%{http_code}' \
                 -H "Authorization: Bearer $BOOTSTRAP_TOKEN" \
                 -H "Content-Type: application/json" \
                 --connect-timeout 5 --max-time 25 \
                 -X POST "$CP_URL/agent/check-in" -d "$body" 2>/dev/null) \
    && _curl_rc=0 || _curl_rc=$?
  resp=$(cat "$_body_file" 2>/dev/null)
  rm -f "$_body_file"

  if [ "$_curl_rc" -ne 0 ]; then
    log_error "check-in network failure (curl rc=$_curl_rc, likely DNS or unreachable). Last body: ${resp:0:120}"
    state_inc check_in_error; write_textfile; return
  fi
  if [ "$_http_code" = "401" ] || [ "$_http_code" = "403" ]; then
    local _detail
    _detail=$(echo "$resp" | jq -r '.detail // "(no detail)"' 2>/dev/null)
    log_error "check-in HTTP $_http_code (auth): $_detail. Token in /etc/drift-deploy/env may be stale (device deleted + re-commissioned)."
    state_inc check_in_error; write_textfile; return
  fi
  if [ "$_http_code" = "409" ]; then
    # Fingerprint mismatch: the CP recorded a different host's
    # /etc/machine-id on the first check-in after commissioning, and
    # this host's fingerprint doesn't match. Almost always means the
    # commissioning curl was pasted on the wrong host (or this host
    # was OS-reinstalled, regenerating /etc/machine-id). Log loudly
    # and DON'T retry — the situation won't fix itself on the next
    # tick, and silent retries would just spam the CP. The container
    # is supervised by --restart unless-stopped; the operator looks
    # at docker logs to see this message and decides what to do.
    local _detail
    _detail=$(echo "$resp" | jq -r '.detail // "(no detail)"' 2>/dev/null)
    log_error "check-in HTTP 409 (fingerprint mismatch): $_detail"
    log_error "this host's fingerprint does not match the one the CP recorded for device '$DEVICE_NAME'."
    log_error "if this is the wrong host, run: docker stop drift-deploy-agent"
    log_error "if this is a deliberate migration, delete the device on the CP and commission under a new name."
    state_inc check_in_error; write_textfile; return
  fi
  if [ "$_http_code" != "200" ]; then
    log_error "check-in HTTP $_http_code: ${resp:0:200}"
    state_inc check_in_error; write_textfile; return
  fi
  # 200 — but verify the body is actually parseable JSON before jq
  # iterates over it. Same Caddy-HTML-on-success case from before.
  if ! echo "$resp" | jq -e '.' >/dev/null 2>&1; then
    log_warn "check-in 200 but non-JSON body (Caddy/nginx interception?): ${resp:0:160}"
    state_inc check_in_error; write_textfile; return
  fi
  state_inc check_in_ok

  # Registry credentials: the CP returns a docker config.json `auths`
  # map; we write it verbatim so docker compose pull (inside this
  # container's CLI) can authenticate against private registries. An
  # empty/missing object means "no creds configured" — leave any
  # existing file alone rather than clobbering it, so operators can
  # still hand-edit if they need a registry the CP doesn't know about.
  local auths
  auths=$(echo "$resp" | jq -c '.registry_credentials // {}' 2>/dev/null)
  if [ -n "$auths" ] && [ "$auths" != "{}" ] && [ "$auths" != "null" ]; then
    mkdir -p /root/.docker
    # Atomic write: jsonify into the parent dir then mv, so a partial
    # write never leaves the file truncated mid-tick.
    local tmp
    tmp=$(mktemp -p /root/.docker config.json.XXXXXX)
    if jq -n --argjson auths "$auths" '{auths: $auths}' > "$tmp" && [ -s "$tmp" ]; then
      chmod 600 "$tmp"
      mv "$tmp" /root/.docker/config.json
    else
      rm -f "$tmp"
      log_warn "could not materialize docker config.json from CP-supplied creds"
    fi
  fi

  # Self-update: if CP says a newer agent.sh is canonical, exit cleanly.
  # Docker's --restart unless-stopped brings us back, and the
  # bootstrapper at the top of the script fetches the new version.
  local target_sha
  target_sha=$(echo "$resp" | jq -r '.agent_target_sha // empty' 2>/dev/null)
  if [ -n "$target_sha" ] && [ "$target_sha" != "$AGENT_SHA" ]; then
    log_info "self-update available: $AGENT_SHA → $target_sha; exiting for Docker restart"
    write_textfile
    # 100 is the self-update sentinel: reconcile_once runs inside the
    # flock subshell, so a plain `exit 0` only exits the subshell — PID 1
    # stays at the same SHA forever. main() looks for this code and exits
    # the outer process, which lets Docker's --restart unless-stopped do
    # its job and the bootstrap fetch the new script.
    exit 100
  fi

  # terminal-bridge.py refresh: replace in place when the served SHA
  # differs. No exec needed — the bridge is forked per terminal session,
  # so the next session picks up the new content. Failure is non-fatal:
  # the in-image baseline keeps working.
  local _bridge_target _bridge_path _bridge_local _bridge_tmp
  _bridge_target=$(echo "$resp" | jq -r '.terminal_bridge_target_sha // empty' 2>/dev/null)
  _bridge_path=/opt/drift/terminal-bridge.py
  if [ -n "$_bridge_target" ] && [ -f "$_bridge_path" ]; then
    _bridge_local=$(sha256sum "$_bridge_path" 2>/dev/null | cut -c1-12)
    if [ "$_bridge_local" != "$_bridge_target" ]; then
      _bridge_tmp=$(mktemp /tmp/terminal-bridge.XXXXXX.py)
      if curl -sS -H "Authorization: Bearer $BOOTSTRAP_TOKEN" \
              --connect-timeout 5 --max-time 20 \
              "$CP_URL/agent/terminal-bridge.py" -o "$_bridge_tmp" 2>/dev/null \
         && [ -s "$_bridge_tmp" ] \
         && python3 -c "import py_compile; py_compile.compile('$_bridge_tmp', doraise=True)" 2>/dev/null; then
        local _new_sha
        _new_sha=$(sha256sum "$_bridge_tmp" | cut -c1-12)
        if [ "$_new_sha" = "$_bridge_target" ]; then
          chmod +x "$_bridge_tmp"
          mv "$_bridge_tmp" "$_bridge_path"
          log_info "terminal-bridge.py updated: $_bridge_local → $_bridge_target"
        else
          rm -f "$_bridge_tmp"
          log_warn "terminal-bridge.py download sha mismatch ($_new_sha vs target $_bridge_target); kept current"
        fi
      else
        rm -f "$_bridge_tmp"
        log_warn "terminal-bridge.py fetch or syntax-check failed; kept current"
      fi
    fi
  fi

  # CP-side facts: refresh the DRIFT_CP_PUBLIC_URL / DRIFT_VM_WRITE_USER /
  # DRIFT_VM_WRITE_PASSWORD values from the response. Update in-memory so
  # this tick's apply phase sees the latest values, AND persist to
  # /etc/drift-deploy/cp-env so the NEXT tick's subshell (which can't
  # inherit shell-var state across the flock) re-sources them at startup.
  local _cp_url _vm_user _vm_pw
  _cp_url=$(echo "$resp" | jq -r '.cp_public_url // empty' 2>/dev/null)
  _vm_user=$(echo "$resp" | jq -r '.vm_write_user // empty' 2>/dev/null)
  _vm_pw=$(echo "$resp" | jq -r '.vm_write_password // empty' 2>/dev/null)
  [ -n "$_cp_url" ]  && DRIFT_CP_PUBLIC_URL="$_cp_url"
  [ -n "$_vm_user" ] && DRIFT_VM_WRITE_USER="$_vm_user"
  [ -n "$_vm_pw" ]   && DRIFT_VM_WRITE_PASSWORD="$_vm_pw"
  write_cp_env "$DRIFT_CP_PUBLIC_URL" "$DRIFT_VM_WRITE_USER" "$DRIFT_VM_WRITE_PASSWORD"

  # Pending terminal sessions: the CP creates a row when an operator
  # clicks "Terminal" in the UI, then surfaces the session id here.
  # Fork one terminal-bridge.py subprocess per session — it owns its
  # own WS connection and pty for the duration of the login. Multiple
  # sessions can run concurrently (independent OS processes, no shared
  # state). If the script is missing (older image), warn and continue
  # so an old agent still reconciles bundles normally.
  if [ -x /opt/drift/terminal-bridge.py ]; then
    echo "$resp" | jq -r '(.pending_sessions // [])[]' 2>/dev/null | while read -r session_id; do
      [ -z "$session_id" ] && continue
      # `/agent/check-in` lives under `$CP_URL` — derive the WS URL by
      # swapping the scheme and the route. Both http:// and https://
      # cases are handled so dev (plain http) and prod (https through
      # Caddy) work identically.
      local ws_url="${CP_URL/http:\/\//ws:\/\/}"
      ws_url="${ws_url/https:\/\//wss:\/\/}"
      ws_url="${ws_url%/}/agent/terminal/ws/${session_id}"
      log_info "spawning terminal bridge for session $session_id"
      # Detach: nohup + & so the bridge outlives this reconcile_once
      # subshell. stdout/stderr go to the agent container's docker logs
      # via the inherited fds, so bridge failures surface in the same
      # place as everything else.
      #
      # 9>&- CRITICAL: closes the inherited flock fd in the child. Without
      # this, the bridge keeps the reconcile lock held for the lifetime
      # of the user's terminal session, which makes the NEXT tick's
      # `flock -w 10` time out and exit 99 → container restart → bridge
      # dies → session closes ≈40s after the user logs in (30s poll +
      # 10s flock wait). Same trap every long-running child inside a
      # flock'd subshell would walk into; fix it at the spawn site.
      nohup /opt/drift/terminal-bridge.py "$ws_url" "$BOOTSTRAP_TOKEN" 9>&- >&2 &
    done
  fi

  local n
  n=$(echo "$resp" | jq -r '(.desired // []) | length' 2>/dev/null)
  n=${n:-0}
  if [ "$n" -gt 0 ] 2>/dev/null; then log_info "check-in: $n app(s) drift from desired"; fi

  # `// []` ensures iteration is safe even if a future CP build
  # accidentally drops the field — null piped to `.[]` is the exact
  # crash mode that put the Pi into a restart spiral on v0.5.4.
  echo "$resp" | jq -c '(.desired // [])[]' 2>/dev/null | while read -r row; do
    local app action rev url sha
    app=$(echo "$row" | jq -r '.app')
    action=$(echo "$row" | jq -r '.action // "deploy"')

    if [ "$action" = "remove" ]; then
      if ! remove_app "$app"; then
        log_error "[$app] remove failed; will retry next tick"
        state_inc apply_error
      fi
      continue
    fi

    if [ "$action" = "restart" ]; then
      # One-shot restart signal from the CP. Run `docker compose
      # restart` against the current revision's directory — no re-pull,
      # no recreate, just SIGTERM-and-restart on every container in the
      # project. The CP has already cleared its pending_restart flag,
      # so a failure here just gets logged; operator re-issues if they
      # want another try.
      rev=$(echo "$row" | jq -r '.revision_id // empty')
      if [ -z "$rev" ]; then
        log_warn "[$app] restart requested with no revision_id; ignoring"
        continue
      fi
      local rev_dir="$APPS_DIR/$app/revisions/$rev"
      if [ ! -d "$rev_dir" ]; then
        log_error "[$app] restart requested but rev_dir missing: $rev_dir"
        state_inc apply_error
        continue
      fi
      log_info "[$app] restart"
      if ! err=$( cd "$rev_dir" \
           && export DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" \
                     DRIFT_APP="$app" DRIFT_DOCKER_DATA_DIR="$DRIFT_DOCKER_DATA_DIR" \
                     DRIFT_HOST_CA_BUNDLE="$DRIFT_HOST_CA_BUNDLE" DRIFT_CP_PUBLIC_URL="$DRIFT_CP_PUBLIC_URL" DRIFT_VM_WRITE_USER="$DRIFT_VM_WRITE_USER" DRIFT_VM_WRITE_PASSWORD="$DRIFT_VM_WRITE_PASSWORD" \
           && docker compose -p "$app" restart 2>&1 ); then
        local err_tail
        err_tail=$(printf '%s' "$err" | tail -n 5 | tr '\n' ' ')
        log_error "[$app] docker compose restart failed: $err_tail"
        state_inc apply_error
      else
        state_inc apply_ok
      fi
      continue
    fi

    rev=$(echo "$row" | jq -r '.revision_id')
    url=$(echo "$row" | jq -r '.bundle_url')
    sha=$(echo "$row" | jq -r '.bundle_sha256')
    if ! apply_revision "$app" "$rev" "$url" "$sha"; then
      log_error "[$app] apply failed; will retry next tick"
      state_inc apply_error
    fi
  done

  write_textfile
}

main() {
  log_info "drift-deploy-agent $AGENT_VERSION (sha:$AGENT_SHA) starting (device=$DEVICE_NAME group=$GROUP_ID interval=${POLL_INTERVAL}s)"
  log_info "blocklist: ${PROTECTED_NAMES[*]}"
  while true; do
    # flock -w 10: wait up to 10s for the lock instead of bailing
    # immediately. Smooths the case where the previous tick takes a
    # little longer than POLL_INTERVAL (slow check-in over flaky WAN).
    # Subshell exit 99 = lock still held after the wait → suppressed
    # unless the lock has been held for >60s (genuine stall).
    ( flock -w 10 9 || exit 99
      date +%s > "$LOCK_ACQUIRED_AT"
      reconcile_once
    ) 9>"$LOCK_FILE"
    rc=$?
    if [ "$rc" -eq 100 ]; then
      # Self-update signal from reconcile_once. Exit PID 1 with 0 so
      # Docker's --restart unless-stopped brings us back fresh; the
      # bootstrap at the top of the script then fetches the new agent.
      exit 0
    fi
    if [ "$rc" -eq 99 ]; then
      acq=$(cat "$LOCK_ACQUIRED_AT" 2>/dev/null || echo 0)
      held=$(( $(date +%s) - acq ))
      if [ "$held" -gt 60 ]; then
        log_warn "previous tick has been running for ${held}s; this tick skipped"
      fi
    fi
    sleep "$POLL_INTERVAL"
  done
}

main "$@"
