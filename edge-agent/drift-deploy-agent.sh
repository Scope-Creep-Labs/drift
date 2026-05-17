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
# Bump on every script change. The check-in payload + textfile metric
# both report this so the control plane can tell at-a-glance which
# devices are running which agent. Companion sha256 (12 chars) computed
# at startup so even if the version is forgotten, the running code can
# always be identified.
AGENT_VERSION="0.5.0"
AGENT_SHA="$(sha256sum "$0" 2>/dev/null | cut -c1-12 || echo unknown)"
LOCK_ACQUIRED_AT="$STATE_DIR/.lock-acquired-at"

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
      printf '    labels:\n'
      printf '      drift.managed: "true"\n'
      printf '      drift.device_name: "%s"\n' "$DRIFT_DEVICE_NAME"
      printf '      drift.group_id: "%s"\n' "$DRIFT_GROUP_ID"
      printf '      drift.app: "%s"\n' "$app"
      printf '      drift.revision: "%s"\n' "$rev_id"
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
    jq -r '.current_revisions | to_entries[] | "drift_deploy_agent_current_revision{app=\"\(.key)\",revision=\"\(.value)\"} 1"' "$STATE_FILE"
  } > "$tmp"
  mv "$tmp" "$TEXTFILE_PATH"
}

apply_revision() {
  local app=$1 rev_id=$2 url=$3 sha=$4
  local rev_dir="$APPS_DIR/$app/$rev_id"
  local bundle="$rev_dir/bundle.tar.gz"
  local err   # last error message, captured for state_set_error

  log_info "[$app] applying revision $rev_id"
  mkdir -p "$rev_dir"

  if ! err=$(curl -fsSL --max-time 120 -o "$bundle" "$url" 2>&1); then
    log_error "[$app] bundle download failed: $err"
    state_set_error "$app" "bundle download failed: $err"
    return 1
  fi
  local got
  got=$(sha256sum "$bundle" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then
    err="sha256 mismatch (got $got, expected $sha)"
    log_error "[$app] $err"
    state_set_error "$app" "$err"
    return 1
  fi
  if ! err=$(tar -xzf "$bundle" -C "$rev_dir" 2>&1); then
    log_error "[$app] extract failed: $err"
    state_set_error "$app" "extract failed: $err"
    return 1
  fi

  if bundle_touches_protected "$rev_dir"; then
    err="REFUSED: bundle touches a protected service/container name — bricking safeguard"
    log_warn "[$app] $err"
    state_set_error "$app" "$err"
    return 1
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
  # Drop the app from current_revisions + clear any pending error.
  atomic_jq "$STATE_FILE" --arg a "$app" \
    'del(.current_revisions[$a]) | (.apply_errors //= {}) | del(.apply_errors[$a])' \
    || log_warn "remove_app: state update failed for $app"
  log_info "[$app] removed"
  state_inc apply_ok
}


reconcile_once() {
  local current errors
  current=$(jq -c '.current_revisions // {}' "$STATE_FILE" 2>/dev/null)
  errors=$(jq -c '.apply_errors // {}' "$STATE_FILE" 2>/dev/null)
  # Belt-and-suspenders: a corrupted state.json from a pre-migration
  # agent could leave these empty. Default to '{}' so --argjson is happy.
  current=${current:-'{}'}
  errors=${errors:-'{}'}
  local body resp
  body=$(jq -n --arg n "$DEVICE_NAME" --arg v "$AGENT_VERSION" --arg g "$GROUP_ID" \
              --argjson c "$current" --argjson e "$errors" \
    '{device_name:$n, agent_version:$v, group_id:$g, current_revisions:$c, apply_errors:$e, health:{}}')

  if ! resp=$(curl_cp -X POST "$CP_URL/agent/check-in" -d "$body"); then
    log_error "check-in failed (network)"
    state_inc check_in_error; write_textfile; return
  fi
  # Caddy / nginx can return non-JSON HTML on 5xx (control plane restart,
  # auth misconfig). Validate before piping to jq so we get a clear log
  # line instead of a spew of jq parse errors + `[ -gt 0 ]` bash failures.
  if ! echo "$resp" | jq -e '.' >/dev/null 2>&1; then
    log_warn "check-in returned non-JSON (CP unhealthy?): ${resp:0:160}"
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

  local n
  n=$(echo "$resp" | jq -r '.desired | length' 2>/dev/null)
  n=${n:-0}
  if [ "$n" -gt 0 ] 2>/dev/null; then log_info "check-in: $n app(s) drift from desired"; fi

  echo "$resp" | jq -c '.desired[]' | while read -r row; do
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
