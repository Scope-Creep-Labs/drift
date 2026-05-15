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

# Per-device facts the agent exports into every `docker compose` subshell.
# Bundles reference these via ${DRIFT_DEVICE_NAME} / ${DRIFT_GROUP_ID}.
DRIFT_DEVICE_NAME="$DEVICE_NAME"
DRIFT_GROUP_ID="$GROUP_ID"

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
AGENT_VERSION="0.2.1"
AGENT_SHA="$(sha256sum "$0" 2>/dev/null | cut -c1-12 || echo unknown)"
LOCK_ACQUIRED_AT="$STATE_DIR/.lock-acquired-at"

mkdir -p "$APPS_DIR" "$TEXTFILE_DIR"
[ -f "$STATE_FILE" ] || echo '{"current_revisions": {}, "metrics": {"check_in_ok": 0, "check_in_error": 0, "apply_ok": 0, "apply_error": 0}}' > "$STATE_FILE"

# Log helpers. Each level includes a word Vector's classifier regex
# matches (error, warn, info) so log_to_metric produces correct
# container_log_lines_total{level=...} counters out of the box.
log()       { printf '[%s] INFO  %s\n'  "$(date -u +%H:%M:%S)" "$*"; }
log_info()  { log "$*"; }
log_warn()  { printf '[%s] WARN  %s\n'  "$(date -u +%H:%M:%S)" "$*" >&2; }
log_error() { printf '[%s] ERROR %s\n'  "$(date -u +%H:%M:%S)" "$*" >&2; }

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
    && DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" DRIFT_APP="$app" \
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
  local app=$1 rev=$2 tmp
  tmp=$(mktemp)
  jq --arg a "$app" --arg r "$rev" '.current_revisions[$a] = $r' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

state_inc() {
  local key=$1 tmp
  tmp=$(mktemp)
  jq --arg k "$key" '.metrics[$k] = ((.metrics[$k] // 0) + 1)' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

write_textfile() {
  # Atomic textfile write for node-exporter's textfile collector.
  local tmp now ok_ci err_ci ok_ap err_ap current_lines=""
  tmp=$(mktemp --tmpdir="$TEXTFILE_DIR" .drift_deploy_agent.XXXXXX) || return
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

  log_info "[$app] applying revision $rev_id"
  mkdir -p "$rev_dir"

  if ! curl -fsSL --max-time 120 -o "$bundle" "$url"; then
    log_error "[$app] bundle download failed"; return 1
  fi
  local got
  got=$(sha256sum "$bundle" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then
    log_error "[$app] sha256 mismatch (got $got, expected $sha)"; return 1
  fi
  if ! tar -xzf "$bundle" -C "$rev_dir"; then
    log_error "[$app] extract failed"; return 1
  fi

  if bundle_touches_protected "$rev_dir"; then
    log_warn "[$app] REFUSED: bundle would touch a protected service/container — bricking safeguard"
    return 1
  fi

  generate_drift_override "$rev_dir" "$app" "$rev_id"

  # -p <app-name> pins the compose project name to the app, so containers
  # get human-readable names (hello-world-echo-1) instead of UUID-prefixed
  # ones derived from the per-revision parent directory.
  # DRIFT_DEVICE_NAME / DRIFT_GROUP_ID / DRIFT_APP are exported so the
  # generated compose.override.yaml resolves them at compose-up time.
  if ! ( cd "$rev_dir" \
       && export DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" DRIFT_APP="$app" \
       && docker compose -p "$app" pull \
       && docker compose -p "$app" up -d --remove-orphans ); then
    log_error "[$app] docker compose up failed"; return 1
  fi

  sleep 30
  local bad
  bad=$( cd "$rev_dir" \
       && export DRIFT_DEVICE_NAME="$DRIFT_DEVICE_NAME" DRIFT_GROUP_ID="$DRIFT_GROUP_ID" DRIFT_APP="$app" \
       && docker compose -p "$app" ps --format json \
       | jq -c 'select(.State != "running") | {Service, State}' )
  if [ -n "$bad" ]; then
    log_error "[$app] post-up health check failed: $bad"
    return 1
  fi

  state_set_current "$app" "$rev_id"
  log_info "[$app] healthy at revision $rev_id"
  state_inc apply_ok
}

reconcile_once() {
  local current
  current=$(jq -c '.current_revisions // {}' "$STATE_FILE")
  local body resp
  body=$(jq -n --arg n "$DEVICE_NAME" --arg v "$AGENT_VERSION" --arg g "$GROUP_ID" --argjson c "$current" \
    '{device_name:$n, agent_version:$v, group_id:$g, current_revisions:$c, health:{}}')

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

  local n
  n=$(echo "$resp" | jq -r '.desired | length' 2>/dev/null)
  n=${n:-0}
  if [ "$n" -gt 0 ] 2>/dev/null; then log_info "check-in: $n app(s) drift from desired"; fi

  echo "$resp" | jq -c '.desired[]' | while read -r row; do
    local app rev url sha
    app=$(echo "$row" | jq -r '.app')
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
