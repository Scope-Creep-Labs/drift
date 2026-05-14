#!/usr/bin/env bash
# Drift Deploy edge agent — v0 (bash + systemd).
#
# Loaded from /etc/drift-deploy/env:
#   DEVICE_NAME, BOOTSTRAP_TOKEN, CP_URL,
#   MANAGED_APPS (comma-separated allowlist), POLL_INTERVAL (default 30s).
#
# Auth model: bearer-only. The Caddy reverse proxy is configured to NOT
# basic_auth /drift/api/deploy/agent/* paths because we can't send both
# Caddy's `Authorization: Basic` and our `Authorization: Bearer` in the
# same request (HTTP Authorization is single-valued).
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
: "${MANAGED_APPS:=}"
: "${POLL_INTERVAL:=30}"

STATE_DIR=/var/lib/drift-deploy
APPS_DIR="$STATE_DIR/apps"
STATE_FILE="$STATE_DIR/state.json"
LOCK_FILE="$STATE_DIR/agent.lock"
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}
TEXTFILE_PATH="$TEXTFILE_DIR/drift_deploy_agent.prom"
AGENT_VERSION="0.1.0"

mkdir -p "$APPS_DIR" "$TEXTFILE_DIR"
[ -f "$STATE_FILE" ] || echo '{"current_revisions": {}, "metrics": {"check_in_ok": 0, "check_in_error": 0, "apply_ok": 0, "apply_error": 0}}' > "$STATE_FILE"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

is_managed() {
  case ",${MANAGED_APPS}," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

curl_cp() {
  curl -sS -H "Authorization: Bearer $BOOTSTRAP_TOKEN" \
       -H "Content-Type: application/json" \
       --connect-timeout 10 --max-time 60 "$@"
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
    printf 'drift_deploy_agent_info{device="%s",version="%s"} 1\n' "$DEVICE_NAME" "$AGENT_VERSION"

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

  log "[$app] applying revision $rev_id"
  mkdir -p "$rev_dir"

  if ! curl -fsSL --max-time 120 -o "$bundle" "$url"; then
    log "[$app] bundle download failed"; return 1
  fi
  local got
  got=$(sha256sum "$bundle" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then
    log "[$app] sha256 mismatch (got $got, expected $sha)"; return 1
  fi
  if ! tar -xzf "$bundle" -C "$rev_dir"; then
    log "[$app] extract failed"; return 1
  fi

  if ! ( cd "$rev_dir" && docker compose pull && docker compose up -d --remove-orphans ); then
    log "[$app] docker compose up failed"; return 1
  fi

  sleep 30
  local bad
  bad=$( cd "$rev_dir" && docker compose ps --format json \
       | jq -c 'select(.State != "running") | {Service, State}' )
  if [ -n "$bad" ]; then
    log "[$app] post-up health check failed: $bad"
    return 1
  fi

  state_set_current "$app" "$rev_id"
  log "[$app] healthy at revision $rev_id"
  state_inc apply_ok
}

reconcile_once() {
  local current
  current=$(jq -c '.current_revisions // {}' "$STATE_FILE")
  local body resp
  body=$(jq -n --arg n "$DEVICE_NAME" --arg v "$AGENT_VERSION" --argjson c "$current" \
    '{device_name:$n, agent_version:$v, current_revisions:$c, health:{}}')

  if ! resp=$(curl_cp -X POST "$CP_URL/agent/check-in" -d "$body"); then
    log "check-in failed"; state_inc check_in_error; write_textfile; return
  fi
  state_inc check_in_ok

  local n
  n=$(echo "$resp" | jq '.desired | length')
  if [ "$n" -gt 0 ]; then log "check-in: $n app(s) drift from desired"; fi

  echo "$resp" | jq -c '.desired[]' | while read -r row; do
    local app rev url sha
    app=$(echo "$row" | jq -r '.app')
    rev=$(echo "$row" | jq -r '.revision_id')
    url=$(echo "$row" | jq -r '.bundle_url')
    sha=$(echo "$row" | jq -r '.bundle_sha256')
    if ! is_managed "$app"; then
      log "[$app] not in MANAGED_APPS allowlist; skipping"; continue
    fi
    if ! apply_revision "$app" "$rev" "$url" "$sha"; then
      log "[$app] apply failed; will retry next tick"
      state_inc apply_error
    fi
  done

  write_textfile
}

main() {
  log "drift-deploy-agent $AGENT_VERSION starting (device=$DEVICE_NAME interval=${POLL_INTERVAL}s)"
  log "MANAGED_APPS=$MANAGED_APPS"
  while true; do
    ( flock -n 9 || { log "another run holds lock; skipping tick"; exit 0; }
      reconcile_once
    ) 9>"$LOCK_FILE" || true
    sleep "$POLL_INTERVAL"
  done
}

main "$@"
