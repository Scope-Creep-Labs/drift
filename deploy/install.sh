#!/usr/bin/env bash
# Drift single-server installer.
#
# Run from the deploy/ directory:
#   ./install.sh
#
# Prompts for: public domain + email (for Let's Encrypt), Drift admin
# user/password, LLM model + matching API key, ntfy topic, optional B2
# credentials.
#
# Auto-generates: Fernet secret key, Postgres password, vmauth reporter
# password, basic-auth password + bcrypt hash for the vmalert/AM gate.
#
# Idempotent: re-running keeps existing values (reads current .env if
# present) and only prompts for missing/empty ones, then renders the
# templated configs and runs `docker compose up -d`.

set -euo pipefail

cd "$(dirname "$0")"

DEPLOY_DIR=$(pwd)
ENV_FILE="$DEPLOY_DIR/.env"
ENV_EXAMPLE="$DEPLOY_DIR/.env.example"

# Persistent state across install-version directories. When you extract
# drift-deploy-0.1.7.tar.gz next to your existing drift-deploy-0.1.6/
# dir, the new install.sh finds the prior .env and answers sidecar at a
# stable host path so prefill works without copying files by hand.
# Override via DRIFT_STATE_DIR=... in the environment if /var/lib isn't
# right for your host.
STATE_DIR="${DRIFT_STATE_DIR:-/var/lib/drift-cp}"
(umask 077 && mkdir -p "$STATE_DIR" "$STATE_DIR/logs")
chmod 700 "$STATE_DIR" "$STATE_DIR/logs" 2>/dev/null || true
ENV_FILE_STATE="$STATE_DIR/.env"
ANSWERS_FILE="$STATE_DIR/logs/last-answers.env"
(umask 077 && touch "$ANSWERS_FILE")
chmod 600 "$ANSWERS_FILE"

# Restore the install dir's .env from state if it's missing — this is
# the cross-version reuse path. If both exist, leave the install dir's
# copy alone (the operator may have hand-edited it between runs); env_get
# reads from it first so manual edits take precedence.
if [ ! -f "$ENV_FILE" ] && [ -f "$ENV_FILE_STATE" ]; then
  (umask 077 && cp -p "$ENV_FILE_STATE" "$ENV_FILE")
  chmod 600 "$ENV_FILE"
  echo "→ restored .env from $STATE_DIR (previous install version)"
fi

# NOTE on umask: we used to set `umask 077` globally here, which had
# a bug — every rendered config in config/ (grafana.ini etc.) inherited
# 600, and grafana's container (uid 472) can't read those. We now use
# `umask 077` only as a brief shield around individual secret-bearing
# writes (.env, last-answers.env, log files) and rely on explicit chmod
# for the rest. Default umask (usually 022) is preserved.
LOG_DIR="$DEPLOY_DIR/logs"
(umask 077 && mkdir -p "$LOG_DIR")
chmod 700 "$LOG_DIR" 2>/dev/null || true

# Tee the entire run to a timestamped log so the operator has a
# permanent record of what was set + what was generated. Mode 600
# because the log captures the prompt feedback (including the
# auto-generated passwords printed in the exit summary). Tee runs
# in a coprocess via process substitution; this works under
# `set -euo pipefail` because the outer shell's pipeline status
# isn't affected by the tee.
LOG_FILE="$LOG_DIR/install-$(date -u +%Y%m%dT%H%M%SZ).log"
(umask 077 && touch "$LOG_FILE")
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "→ logging this run to $LOG_FILE"

# Arrays + state referenced inside the EXIT trap. Declared up here so
# they're guaranteed-defined even if the script errors before reaching
# the section that populates them (set -u would otherwise trip the
# trap itself).
GENERATED_SECRETS=()
COMPOSE_ARGS=()

# Print a summary block on EVERY exit (success, error, Ctrl-C). On
# success the inline output already showed URLs + healthcheck; the
# trap just prints the log path. On error, the trap is the only place
# the operator sees recoverable state — .env path, generated creds,
# current container status — so we print all of that here.
on_exit() {
  local rc=$1
  echo
  if [ "$rc" -ne 0 ]; then
    echo "════════════════════════════════════════════════════════════════════"
    echo "  ✗ install exited with status $rc — partial state below"
    echo "════════════════════════════════════════════════════════════════════"
    [ -f "$ENV_FILE" ]                         && echo "  .env:          $ENV_FILE  (mode 600)"
    [ -f "$ANSWERS_FILE" ] && [ -s "$ANSWERS_FILE" ] && \
                                                  echo "  prompt answers so far: $ANSWERS_FILE"
    if [ "${#GENERATED_SECRETS[@]}" -gt 0 ]; then
      echo
      echo "  Credentials generated this run — save these:"
      for s in "${GENERATED_SECRETS[@]}"; do
        echo "    $s"
      done
    fi
    if docker compose "${COMPOSE_ARGS[@]}" ps -q 2>/dev/null | grep -q .; then
      echo
      echo "  Container state at exit:"
      docker compose "${COMPOSE_ARGS[@]}" ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null | sed 's/^/    /'
    fi
    echo
  fi
  echo "Full install log: $LOG_FILE"
  exit "$rc"
}
trap 'on_exit $?' EXIT

# ---------- helpers ----------

err() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "warn:  $*" >&2; }
info() { echo "       $*"; }
heading() { echo; echo "═══ $* ═══"; }

# Read a single value, preferring .env (the canonical source) and
# falling back to the in-progress sidecar at logs/.last-answers.env.
# The sidecar survives anything that nukes .env between runs (mode
# switch, manual rm, mid-script abort), so prefill is robust.
env_get() {
  local key=$1 line=""
  if [ -f "$ENV_FILE" ]; then
    line=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 || true)
  fi
  if [ -z "$line" ] && [ -f "$ANSWERS_FILE" ]; then
    # Tail -1 so the *last* save wins (we append-and-dedupe on save).
    line=$(grep -E "^${key}=" "$ANSWERS_FILE" 2>/dev/null | tail -1 || true)
  fi
  [ -z "$line" ] && { echo ""; return; }
  echo "${line#${key}=}"
}

# Persist a single KEY=VALUE to the sidecar immediately, so prefill
# survives a mid-script abort. Strips any prior entry for the same
# key, then appends — order in the file doesn't matter (env_get
# tail -1's per key).
save_answer() {
  local key=$1 value=$2
  [ -f "$ANSWERS_FILE" ] || { touch "$ANSWERS_FILE" && chmod 600 "$ANSWERS_FILE"; }
  # Drop any existing entry for this key (sed in-place; portable across GNU + BSD).
  if grep -qE "^${key}=" "$ANSWERS_FILE" 2>/dev/null; then
    grep -vE "^${key}=" "$ANSWERS_FILE" > "${ANSWERS_FILE}.tmp" && mv "${ANSWERS_FILE}.tmp" "$ANSWERS_FILE"
    chmod 600 "$ANSWERS_FILE"
  fi
  printf '%s=%s\n' "$key" "$value" >> "$ANSWERS_FILE"
}

# Prompt for a value, accepting Enter to keep the current value.
ask() {
  local key=$1 prompt=$2
  local default=${3:-}
  local current
  current=$(env_get "$key")
  local hint
  if [ -n "$current" ]; then
    hint=" [current: $current]"
  elif [ -n "$default" ]; then
    hint=" [default: $default]"
  else
    hint=""
  fi
  local answer
  read -rp "$prompt$hint: " answer
  if [ -n "$answer" ]; then
    eval "$key=\"\$answer\""
  elif [ -n "$current" ]; then
    eval "$key=\"\$current\""
  else
    eval "$key=\"\$default\""
  fi
  local resolved
  eval "resolved=\$$key"
  save_answer "$key" "$resolved"
}

# Same as ask() but for secrets — echo is disabled and we don't show
# the current value (only a "(unchanged)" hint when present).
ask_secret() {
  local key=$1 prompt=$2
  local current
  current=$(env_get "$key")
  local hint=""
  [ -n "$current" ] && hint=" [Enter to keep current]"
  local answer
  read -rsp "$prompt$hint: " answer
  echo
  if [ -n "$answer" ]; then
    eval "$key=\"\$answer\""
  else
    eval "$key=\"\$current\""
  fi
  local resolved
  eval "resolved=\$$key"
  save_answer "$key" "$resolved"
}

# Secret-or-autogen: prompt for a password with three behaviors:
#   - Enter        → keep current if one exists, else auto-generate.
#   - "!"          → explicit rotation (force fresh auto-gen).
#   - any other    → use the typed value verbatim.
# Tracks generated values in $GENERATED_SECRETS (declared above the
# EXIT trap so the trap can read it safely under set -u) for the
# "save these" exit summary.
ask_secret_autogen() {
  local key=$1 prompt=$2 length=${3:-20}
  local current
  current=$(env_get "$key")
  local hint
  if [ -n "$current" ]; then
    hint=" [Enter=keep · ! to rotate · or type new]"
  else
    hint=" [Enter to auto-generate · or type new]"
  fi
  local answer
  read -rsp "$prompt$hint: " answer
  echo
  case "$answer" in
    "")
      if [ -n "$current" ]; then
        eval "$key=\"\$current\""
      else
        local generated
        generated=$(rand_token "$length")
        eval "$key=\"\$generated\""
        GENERATED_SECRETS+=("$key=$generated")
      fi
      ;;
    "!")
      local generated
      generated=$(rand_token "$length")
      eval "$key=\"\$generated\""
      GENERATED_SECRETS+=("$key=$generated  (rotated)")
      ;;
    *)
      eval "$key=\"\$answer\""
      ;;
  esac
  local resolved
  eval "resolved=\$$key"
  save_answer "$key" "$resolved"
}

# Generate a URL-safe random string. Used for passwords + secret keys
# that don't have a fixed format requirement.
rand_token() {
  local n=${1:-24}
  head -c $((n * 2)) /dev/urandom | base64 | tr -d '/+=\n' | head -c "$n"
}

# Generate a Fernet key (32 bytes urlsafe base64). Required for the
# drift secrets subsystem; format is enforced by the cryptography lib.
gen_fernet() {
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
    || head -c 32 /dev/urandom | base64 | tr '+/' '-_'
}

# bcrypt-hash a password for Caddy's basic_auth directive. Uses Caddy
# itself (we already need the image) so we don't add a python crypt dep.
bcrypt_caddy() {
  local plain=$1
  docker run --rm caddy:2 caddy hash-password --plaintext "$plain"
}

# ---------- preflight ----------

heading "Preflight"
command -v docker >/dev/null || err "docker not installed"
docker compose version >/dev/null 2>&1 || err "docker compose plugin missing"
info "docker $(docker --version | awk '{print $3}' | tr -d ',')"
info "compose $(docker compose version | awk '{print $4}')"
info "deploy dir: $DEPLOY_DIR"
info "state dir:  $STATE_DIR  (persistent across install-version dirs)"

if [ -f "$ENV_FILE" ]; then
  info "found existing .env (will prompt to keep or change values)"
elif [ -s "$ANSWERS_FILE" ]; then
  info "found prior answers in $ANSWERS_FILE (will prefill from there)"
fi

# ---------- prompts ----------

heading "Reverse proxy / TLS"
echo "  Drift's services expose ports on 127.0.0.1 for an external"
echo "  reverse proxy (Caddy/Traefik/nginx) to front. The bundle includes"
echo "  a Caddy service that does this automatically with Let's Encrypt"
echo "  TLS — opt in if you don't already run a reverse proxy on this box."
ask USE_BUNDLED_CADDY "Use the bundled Caddy for TLS? [y/N]" "n"
case "${USE_BUNDLED_CADDY:-n}" in
  y|Y|yes|YES|true|1) USE_BUNDLED_CADDY=true ;;
  *)                  USE_BUNDLED_CADDY=false ;;
esac

if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  ask DOMAIN              "Public hostname (must already resolve to this host's IP)" drift.example.com
  ask LETSENCRYPT_EMAIL   "Email for Let's Encrypt notices (rare; can be left blank)" ""
  PUBLIC_URL="https://${DOMAIN}"
  save_answer PUBLIC_URL "$PUBLIC_URL"
else
  # External reverse proxy mode. We still need a PUBLIC_URL for
  # ALLOWED_ORIGINS (browser → drift-agent CORS) and for vmalert /
  # alertmanager's --web.external-url (link generation). No default —
  # the operator knows their setup; we just need an explicit value.
  echo "  Examples:"
  echo "    https://drift.example.com         (Drift at root of its own subdomain)"
  echo "    https://example.com/drift         (Drift at /drift on an existing domain)"
  while true; do
    ask PUBLIC_URL "Public URL the Drift web UI will be reached at" ""
    case "$PUBLIC_URL" in
      http://*|https://*) break ;;
      "") warn "Required — must start with http:// or https://" ;;
      *)  warn "Must start with http:// or https://" ;;
    esac
  done
  # Pull domain out of the public URL for templates that need just the host.
  DOMAIN="${PUBLIC_URL#https://}"; DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN%%/*}"
  LETSENCRYPT_EMAIL=""
  save_answer DOMAIN "$DOMAIN"
  save_answer LETSENCRYPT_EMAIL ""
  ask DRIFT_HOST_PORT "Local port to bind drift-frontend on (127.0.0.1:<port>)" 10001
fi

# PATH_PREFIX: the path component of PUBLIC_URL (e.g. "/drift" when
# PUBLIC_URL=https://example.com/drift; empty for root deployments).
# Persisted to .env so docker-compose.yml can interpolate it into
# intra-network URLs that need the same prefix as the public route —
# specifically vmalert's --notifier.url and ALERTMANAGER_URL, since
# alertmanager's web.route-prefix is derived from --web.external-url
# = PUBLIC_URL/am.
_path_after_host="${PUBLIC_URL#*://}"
_path_after_host="${_path_after_host#"$DOMAIN"}"
_path_after_host="${_path_after_host%/}"
PATH_PREFIX="$_path_after_host"
save_answer PATH_PREFIX "$PATH_PREFIX"

# Basic-auth gate on the raw vmalert + Alertmanager web UIs. Only
# asked when the bundled Caddy is in use — in external-proxy mode the
# operator's existing reverse proxy handles auth however it wants
# (basic_auth / OAuth / mTLS / etc.) and we'd be pulling the caddy
# image just to bcrypt. The rendered Caddyfile.sample in external mode
# shows the path routing without a basic_auth block; operator adds
# their own.
if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  heading "vmalert / Alertmanager UI password"
  echo "  Gates the raw vmalert + Alertmanager web UIs at /vmalert/ and /am/."
  ask WEB_AUTH_USER "Username (basic-auth)" drift
  ask_secret_autogen WEB_AUTH_PASSWORD_PLAINTEXT "Password (basic-auth)"
else
  WEB_AUTH_USER=""
  WEB_AUTH_PASSWORD_PLAINTEXT=""
  WEB_AUTH_HASH=""
fi

heading "Drift admin"
ask DRIFT_ADMIN_USERNAME "Drift admin username" admin
ask_secret_autogen DRIFT_ADMIN_PASSWORD "Drift admin password"

# Validate an LLM API key against the provider's `/models` endpoint.
# Returns 0 on success, 1 on auth failure, 2 on network error.
# Doesn't echo the key — only the status code.
validate_llm_key() {
  local provider=$1 key=$2 code
  case "$provider" in
    anthropic)
      code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 \
        -H "x-api-key: $key" -H "anthropic-version: 2023-06-01" \
        https://api.anthropic.com/v1/models)
      ;;
    openai)
      code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 \
        -H "Authorization: Bearer $key" \
        https://api.openai.com/v1/models)
      ;;
    gemini)
      # Gemini's API key is a URL param. Use a different host probe so
      # we don't leak the key in process listings.
      code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 \
        "https://generativelanguage.googleapis.com/v1beta/models?key=$key")
      ;;
    *) return 2 ;;
  esac
  case "$code" in
    200) return 0 ;;
    000) return 2 ;;
    *)   echo "  ↳ provider returned HTTP $code" >&2; return 1 ;;
  esac
}

# Prompt for the API key + sanity-check it against the provider's
# /models endpoint. Soft validation only — we WARN on failure but
# never abort, and we never modify .env mid-script (an earlier
# version did and clobbered valid keys when the API returned a
# transient 4xx). Operator can re-run install.sh to change a key
# without risk of losing the previous value.
ask_and_validate_llm_key() {
  local key_name=$1 provider=$2 prompt=$3
  ask_secret "$key_name" "$prompt"
  local key
  eval "key=\$$key_name"
  if [ -z "$key" ]; then
    warn "API key is empty — chat will fail until you set $key_name in .env."
    return
  fi
  echo -n "  validating against $provider… "
  if validate_llm_key "$provider" "$key"; then
    echo "✓ key works"
    return
  fi
  local rc=$?
  if [ "$rc" = 2 ]; then
    echo "(couldn't reach provider — accepting key, verify after install)"
    return
  fi
  echo "✗ rejected"
  warn "  $provider rejected the key. Saving anyway; if chat fails after install,"
  warn "  edit $key_name in .env or re-run install.sh to enter a new one."
}

heading "LLM"
echo "  Pick the model Drift's agent will run. The matching API key is asked next."
echo "  Common picks: claude-opus-4-7 | gpt-5.4-mini | gpt-4o | o3 | gemini-2.5-pro"
ask MODEL "Model id" claude-opus-4-7
ask EFFORT "Reasoning effort (low/medium/high)" medium
ask MAX_TOKENS "Max output tokens per call" 64000
# Only prompt for + validate the key that matches the chosen model's provider.
case "$MODEL" in
  claude-*|*/claude-*) ask_and_validate_llm_key ANTHROPIC_API_KEY anthropic "Anthropic API key" ;;
  gpt-*|o1*|o3*|*/gpt-*|*/o1*|*/o3*) ask_and_validate_llm_key OPENAI_API_KEY openai "OpenAI API key" ;;
  gemini-*|*/gemini-*) ask_and_validate_llm_key GEMINI_API_KEY gemini "Gemini API key" ;;
  *) warn "Unknown model prefix '$MODEL' — set the right *_API_KEY in .env manually after install." ;;
esac

heading "ntfy push (Alertmanager → phone)"
echo "  Pick any unique-ish topic; subscribe to https://ntfy.sh/<topic> on your phone."
DEFAULT_NTFY="drift-$(rand_token 8)"
ask NTFY_TOPIC "ntfy topic" "$DEFAULT_NTFY"

heading "Bundle storage (for Drift Deploy compose bundles)"
echo "  When you deploy an app, Drift packs its compose files into a"
echo "  tar.gz bundle that the edge-agent on each device pulls down."
echo "  Default 'local' stores those bundles on this host (the Drift"
echo "  control plane, or CP) and serves them to devices directly."
echo "  Switch to 's3' to push bundles to an external bucket (B2/AWS/"
echo "  MinIO) — useful when multiple CPs share one bundle store."
ask BUNDLE_STORAGE "Backend: local | s3" "local"
if [ "$BUNDLE_STORAGE" = "s3" ]; then
  ask B2_ENDPOINT      "S3 endpoint URL" "https://s3.us-west-002.backblazeb2.com"
  ask B2_REGION        "S3 region" "us-west-002"
  ask B2_ACCESS_KEY_ID "S3 access key id" ""
  ask_secret B2_SECRET_ACCESS_KEY "S3 secret access key"
  ask B2_BUCKET        "S3 bucket name" ""
else
  B2_ENDPOINT=""; B2_REGION=""; B2_ACCESS_KEY_ID=""
  B2_SECRET_ACCESS_KEY=""; B2_BUCKET=""
  # Override any sidecar values from a previous s3 run so we don't
  # silently re-suggest stale credentials on the next storage-mode flip.
  save_answer B2_ENDPOINT ""
  save_answer B2_REGION ""
  save_answer B2_ACCESS_KEY_ID ""
  save_answer B2_SECRET_ACCESS_KEY ""
  save_answer B2_BUCKET ""
fi

heading "Self-scrape (reporter on this host)"
ask REPORTER_HOSTNAME "Hostname label for self-scraped metrics" "$(hostname -s 2>/dev/null || echo drift-host)"
ask REPORTER_GROUP    "Group label for self-scraped metrics" cloud

heading "Auto-generated secrets"
echo "  Drift Postgres password, Fernet key, and the vmauth reporter"
echo "  password are auto-generated on first install and silently"
echo "  preserved on rerun. To rotate one, answer '!' at the prompt"
echo "  (others stay untouched). Press Enter to keep current."

rotate_or_keep() {
  # Args: KEY  LABEL  GENERATOR_FN
  # Reads current via env_get; prompts only if a current value
  # exists. On fresh install (current empty), silently generates.
  local key=$1 label=$2 gen_fn=$3
  local current
  current=$(env_get "$key")
  if [ -z "$current" ]; then
    local fresh
    fresh=$($gen_fn)
    eval "$key=\"\$fresh\""
    info "generated $key ($label)"
    GENERATED_SECRETS+=("$key=$fresh")
    save_answer "$key" "$fresh"
    return
  fi
  local answer
  read -rp "  Rotate $key? Type ! to rotate, Enter to keep current: " answer
  if [ "$answer" = "!" ]; then
    local fresh
    fresh=$($gen_fn)
    eval "$key=\"\$fresh\""
    info "rotated $key"
    GENERATED_SECRETS+=("$key=$fresh  (rotated)")
    save_answer "$key" "$fresh"
  else
    eval "$key=\"\$current\""
    info "kept existing $key"
    save_answer "$key" "$current"
  fi
}
_gen_pw() { rand_token 24; }
rotate_or_keep DRIFT_PG_PASSWORD   "Postgres"          _gen_pw
rotate_or_keep DRIFT_SECRET_KEY    "Fernet key"        gen_fernet
rotate_or_keep REPORTER_PASSWORD   "vmauth reporter"   _gen_pw

if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  heading "Hashing vmalert/AM UI password (bcrypt via caddy:2)"
  WEB_AUTH_HASH=$(bcrypt_caddy "$WEB_AUTH_PASSWORD_PLAINTEXT")
  info "bcrypt hash generated"
  # Compose interpolates `$X` syntax inside .env values. Bcrypt hashes
  # start with `$2a$14$...` which compose would otherwise read as three
  # variable references. Double the dollars so compose treats them
  # literally — caddy still sees the original hash because compose
  # un-escapes `$$` → `$` when it injects the value into the container's
  # env. Same trick docker-compose.yml uses for $$ in command args.
  WEB_AUTH_HASH_ENV=${WEB_AUTH_HASH//$/$$}
else
  WEB_AUTH_HASH_ENV=""
fi
# Sidecar copies for prefill on rerun (no-op in external mode — all empty).
save_answer WEB_AUTH_USER "$WEB_AUTH_USER"
save_answer WEB_AUTH_PASSWORD_PLAINTEXT "$WEB_AUTH_PASSWORD_PLAINTEXT"
save_answer WEB_AUTH_HASH "$WEB_AUTH_HASH_ENV"

# ---------- write .env ----------

# Detect the host docker.sock's group gid so drift-agent's `app` user
# can be added to a matching supplementary group at runtime (needed for
# the admin update-apply path that talks to the daemon over the socket).
# Falls back to 999 — the gid the slim image's `app` user already has,
# which is harmless if the host doesn't match (the apply endpoint just
# returns a permission error in that case).
_DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 999)

heading "Writing .env"
# Scope umask 077 to the .env heredoc only — leaks into render() if
# set globally and we end up with config/grafana.ini at mode 600,
# which grafana (uid 472) can't read. The chmod 600 below is the
# real guarantee; the in-subshell umask just closes the brief window
# between cat opening the file and chmod running.
(umask 077 && cat > "$ENV_FILE" <<EOF
# Generated by install.sh — re-run install.sh to update.
USE_BUNDLED_CADDY=$USE_BUNDLED_CADDY
DOMAIN=$DOMAIN
LETSENCRYPT_EMAIL=$LETSENCRYPT_EMAIL
PUBLIC_URL=$PUBLIC_URL
# Path component of PUBLIC_URL (empty for root deployments, "/drift"
# etc. for path-prefix). vmalert + drift-agent use it to address
# alertmanager intra-network at the matching route-prefix.
PATH_PREFIX=$PATH_PREFIX
# Real host path of this install dir, bind-mounted into drift-agent at
# /host-deploy. Used by /api/admin/updates/apply so compose's
# --project-directory points at the daemon-visible path (so bind
# mounts like ./config/alerts resolve correctly).
DEPLOY_DIR=$DEPLOY_DIR
# Host docker.sock gid — supplemental group for drift-agent's app user
# so it can talk to the daemon socket for the update-apply endpoint.
DOCKER_GID=$_DOCKER_GID
DRIFT_HOST_PORT=${DRIFT_HOST_PORT:-10001}
VMALERT_HOST_PORT=${VMALERT_HOST_PORT:-8880}
ALERTMANAGER_HOST_PORT=${ALERTMANAGER_HOST_PORT:-9093}
GRAFANA_HOST_PORT=${GRAFANA_HOST_PORT:-3000}
VMAUTH_HOST_PORT=${VMAUTH_HOST_PORT:-8427}

WEB_AUTH_USER=$WEB_AUTH_USER
# Plaintext kept here so re-running install.sh prefills the prompt
# instead of silently rotating the password. .env is mode 600.
WEB_AUTH_PASSWORD_PLAINTEXT=$WEB_AUTH_PASSWORD_PLAINTEXT
WEB_AUTH_HASH=$WEB_AUTH_HASH_ENV

MODEL=$MODEL
EFFORT=$EFFORT
MAX_TOKENS=$MAX_TOKENS
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
GEMINI_API_KEY=${GEMINI_API_KEY:-}

DRIFT_ADMIN_USERNAME=$DRIFT_ADMIN_USERNAME
DRIFT_ADMIN_PASSWORD=$DRIFT_ADMIN_PASSWORD

DRIFT_PG_USER=drift
DRIFT_PG_DB=drift
DRIFT_PG_PASSWORD=$DRIFT_PG_PASSWORD

DRIFT_SECRET_KEY=$DRIFT_SECRET_KEY

NTFY_TOPIC=$NTFY_TOPIC

REPORTER_PASSWORD=$REPORTER_PASSWORD
REPORTER_HOSTNAME=$REPORTER_HOSTNAME
REPORTER_GROUP=$REPORTER_GROUP

VM_RETENTION=90d
VL_RETENTION=30d

BUNDLE_STORAGE=${BUNDLE_STORAGE:-local}
B2_ENDPOINT=${B2_ENDPOINT:-}
B2_REGION=${B2_REGION:-}
B2_ACCESS_KEY_ID=${B2_ACCESS_KEY_ID:-}
B2_SECRET_ACCESS_KEY=${B2_SECRET_ACCESS_KEY:-}
B2_BUCKET=${B2_BUCKET:-}
B2_PREFIX=drift-bundles

VM_BASIC_AUTH=
VM_BEARER_TOKEN=
EOF
)
chmod 600 "$ENV_FILE"
# Mirror to the persistent state dir so the next install-version
# extract (different DEPLOY_DIR) finds the same values.
(umask 077 && cp -p "$ENV_FILE" "$ENV_FILE_STATE")
chmod 600 "$ENV_FILE_STATE"
info ".env written ($(wc -l < "$ENV_FILE") lines, mode 600 · mirrored to $STATE_DIR)"

# ---------- render config templates ----------

heading "Rendering config templates"
render() {
  local src=$1 dst=$2
  shift 2
  # Pairs of __KEY__ value [__KEY__ value …]; rendered via sed.
  local input
  input=$(cat "$src")
  while [ $# -gt 0 ]; do
    local key=$1 val=$2
    shift 2
    # Escape sed delimiter (|) and ampersand in replacement.
    local esc=${val//\\/\\\\}
    esc=${esc//|/\\|}
    esc=${esc//&/\\&}
    input=$(printf '%s' "$input" | sed "s|$key|$esc|g")
  done
  printf '%s\n' "$input" > "$dst"
  info "  rendered $dst"
}

if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  render config/Caddyfile.tmpl              config/Caddyfile \
    __DOMAIN__                "$DOMAIN" \
    __LETSENCRYPT_EMAIL__     "${LETSENCRYPT_EMAIL:-off}" \
    __WEB_AUTH_USER__         "$WEB_AUTH_USER" \
    __WEB_AUTH_HASH__         "$WEB_AUTH_HASH"
else
  # External reverse-proxy mode. Render one sample per common reverse
  # proxy so the operator picks the right one for their existing
  # setup. All three use 127.0.0.1:<port> upstreams that match the
  # loopback bindings in docker-compose.external.yml. No auth blocks
  # by default — operator's existing proxy handles auth however it
  # already does.
  # PATH_PREFIX is already derived above (right after PUBLIC_URL is
  # finalized) and persisted to .env. The reverse-proxy templates
  # consume the same substitution so their rules align with whichever
  # URL layout the operator picked at the PUBLIC_URL prompt.
  _sub=(
    __DOMAIN__                "$DOMAIN"
    __PATH_PREFIX__           "$PATH_PREFIX"
    __DRIFT_HOST_PORT__       "${DRIFT_HOST_PORT:-10001}"
    __VMALERT_HOST_PORT__     "${VMALERT_HOST_PORT:-8880}"
    __ALERTMANAGER_HOST_PORT__ "${ALERTMANAGER_HOST_PORT:-9093}"
    __GRAFANA_HOST_PORT__     "${GRAFANA_HOST_PORT:-3000}"
    __VMAUTH_HOST_PORT__      "${VMAUTH_HOST_PORT:-8427}"
  )
  render config/Caddyfile.external.tmpl  config/Caddyfile.sample  "${_sub[@]}"
  render config/nginx.external.tmpl      config/nginx.conf.sample "${_sub[@]}"
  render config/traefik.external.tmpl    config/traefik.yml.sample "${_sub[@]}"
  info "  reverse-proxy samples written:"
  info "    - config/Caddyfile.sample"
  info "    - config/nginx.conf.sample"
  info "    - config/traefik.yml.sample"
fi

render config/auth.yml.tmpl                 config/auth.yml \
  __REPORTER_PASSWORD__     "$REPORTER_PASSWORD"

render config/alertmanager-ntfy.yml.tmpl    config/alertmanager-ntfy.yml \
  __NTFY_TOPIC__             "$NTFY_TOPIC"

# Grafana's external URL depends on whether we're root-serving (with
# bundled Caddy) or sitting behind an external reverse proxy. PUBLIC_URL
# already encodes the right host; grafana.ini.tmpl handles the path.
render config/grafana.ini.tmpl              config/grafana.ini \
  __DOMAIN__                "${DOMAIN:-${PUBLIC_URL#https://}}"

# Ensure the alerts and alertmanager dirs are writable by the drift-agent
# container's `app` user (uid 999 inside the image). vmalert + Alertmanager
# still read fine.
chown -R 999:999 config/alerts config/alertmanager 2>/dev/null || \
  warn "couldn't chown config/alerts + config/alertmanager (run as root?)"
chmod -R u+rwX,g+rX,o+rX config/alerts config/alertmanager

# ---------- launch ----------

heading "Launching"
# Mode picks the compose-file set:
#   bundled Caddy:  only docker-compose.yml + the `caddy` profile.
#                   Services stay docker-network-only (no host ports
#                   bound) — Caddy reaches them via service DNS.
#   external proxy: layer docker-compose.external.yml on top to bind
#                   127.0.0.1:<port> for each service the external
#                   reverse proxy needs to reach.
if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  COMPOSE_ARGS=(--profile caddy)
else
  COMPOSE_ARGS=(-f docker-compose.yml -f docker-compose.external.yml)
fi
docker compose "${COMPOSE_ARGS[@]}" pull
docker compose "${COMPOSE_ARGS[@]}" up -d
echo

# ---------- health check ----------
heading "Status"
# drift-agent + drift-postgres take a moment: alembic runs migrations,
# the API key is validated, the admin user is bootstrapped. Poll until
# everything is running + healthy, up to a 90s deadline. Exits the
# wait loop the moment the stack is clean, so a fast install doesn't
# pay the full 90s.
echo "  waiting for services to settle (up to 90s)..."
deadline=$((SECONDS + 90))
while [ $SECONDS -lt $deadline ]; do
  bad=$(docker compose "${COMPOSE_ARGS[@]}" ps --format "{{.State}}|{{.Status}}" 2>/dev/null \
        | awk -F'|' 'NF>0 && ($1 != "running" || $2 ~ /unhealthy|[Rr]estarting|health: starting/)' \
        | wc -l)
  [ "${bad:-1}" -eq 0 ] && break
  sleep 3
done
echo

# Final state table for the operator.
docker compose "${COMPOSE_ARGS[@]}" ps --format "table {{.Name}}\t{{.Status}}"
echo

# Classify each container. "running + healthy" or "running + Up X minutes"
# (no healthcheck) → OK. Anything restarting/unhealthy/exited → show
# the last 10 log lines inline so the operator can diagnose without
# hunting for `docker logs`.
unhappy=()
while IFS='|' read -r name state status; do
  [ -z "$name" ] && continue
  if [ "$state" != "running" ] || echo "$status" | grep -qE "unhealthy|[Rr]estarting"; then
    unhappy+=("$name"$'\t'"$status")
  fi
done < <(docker compose "${COMPOSE_ARGS[@]}" ps --format "{{.Name}}|{{.State}}|{{.Status}}" 2>/dev/null)

if [ ${#unhappy[@]} -eq 0 ]; then
  echo "✓ all services healthy"
else
  warn "${#unhappy[@]} container(s) are not healthy:"
  for entry in "${unhappy[@]}"; do
    name=${entry%%$'\t'*}
    status=${entry#*$'\t'}
    echo
    echo "  ── $name  ($status)"
    docker logs "$name" --tail 10 2>&1 | sed 's/^/      /'
  done
  echo
  echo "  Re-check with:  docker compose ps  +  docker logs <name>"
  echo "  Drift web UI may still load if drift-agent, drift-postgres, and"
  echo "  drift-frontend are healthy. If drift-agent is restarting on"
  echo "  'InvalidPasswordError', there's a stale postgres volume — see"
  echo "  $DEPLOY_DIR/README.md (troubleshooting)."
fi
echo

# ---------- self-device bootstrap ----------
# Commission the CP host itself as a managed device so it shows up in
# the Devices list and apps (the default reporter etc.) can be deployed
# to it like any other device. Uses REPORTER_HOSTNAME + REPORTER_GROUP
# from .env so the device's identity matches its self-scraped metrics.
#
# Hits the CP via 127.0.0.1 (loopback in external-proxy mode) or via
# localhost over Caddy (bundled mode) — avoids DNS/TLS round-trips
# through the public URL right after compose-up, which might still be
# warming up. The edge-agent install_cmd returned by the API does use
# PUBLIC_URL because the resulting container needs to reach the CP
# over its real network path.

heading "Bootstrapping CP as a managed device"

# Choose internal URL: loopback for external mode, https://localhost
# (with -k to skip TLS verify while LE may still be provisioning) for
# bundled mode.
if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  _api_local="https://localhost"
  _curl_opts=("-k")
else
  _api_local="http://127.0.0.1:${DRIFT_HOST_PORT:-10001}"
  _curl_opts=()
fi

# Wait briefly for /api/auth/me to respond (200 or 401 — both prove the
# stack is serving). Skips with a warn after ~20s.
_self_ok=false
for _i in 1 2 3 4 5 6 7 8 9 10; do
  _code=$(curl -sS "${_curl_opts[@]}" -o /dev/null -w "%{http_code}" --max-time 3 \
    "$_api_local/api/auth/me" 2>/dev/null || echo "000")
  case "$_code" in
    200|401) _self_ok=true; break ;;
  esac
  sleep 2
done

if [ "$_self_ok" = "false" ]; then
  warn "CP API not reachable on $_api_local — skipping self-bootstrap"
  warn "  Commission later via chat: 'add device $REPORTER_HOSTNAME to group $REPORTER_GROUP'"
else
  _cookies=$(mktemp)
  _login_body=$(mktemp)
  _device_body=$(mktemp)

  _login_code=$(curl -sS "${_curl_opts[@]}" -o "$_login_body" -w "%{http_code}" \
    -c "$_cookies" -H "Content-Type: application/json" \
    -d "{\"username\":\"$DRIFT_ADMIN_USERNAME\",\"password\":\"$DRIFT_ADMIN_PASSWORD\"}" \
    "$_api_local/api/auth/login")

  if [ "$_login_code" != "200" ]; then
    warn "admin login failed (HTTP $_login_code): $(head -c 200 "$_login_body" 2>/dev/null)"
    warn "  Skipping self-bootstrap. Commission later via chat."
  else
    _device_code=$(curl -sS "${_curl_opts[@]}" -o "$_device_body" -w "%{http_code}" \
      -b "$_cookies" -H "Content-Type: application/json" \
      -d "{\"name\":\"$REPORTER_HOSTNAME\",\"group_id\":\"$REPORTER_GROUP\"}" \
      "$_api_local/api/deploy/devices")

    case "$_device_code" in
      201)
        info "device commissioned: $REPORTER_HOSTNAME (group=$REPORTER_GROUP)"
        _install_cmd=$(jq -r '.install_cmd' < "$_device_body" 2>/dev/null)
        if [ -n "$_install_cmd" ] && [ "$_install_cmd" != "null" ]; then
          info "running edge-agent install for self-device..."
          # eval so the env-prefixed pipeline expands as intended. The
          # install.sh fetched here is served by the CP we just brought
          # up — same host, same image.
          #
          # DRIFT_INSTALL_ASSUME_YES bypasses the edge-agent installer's
          # interactive "Proceed?" prompt. We just did a complete summary
          # of the larger install one screen up; the operator already
          # consented to commissioning the CP host.
          if DRIFT_INSTALL_ASSUME_YES=1 eval "$_install_cmd"; then
            info "edge-agent running on the CP — it should appear online within ~30s"
          else
            warn "edge-agent install failed; CP is registered but no agent running. Re-run with:"
            warn "  $_install_cmd"
          fi
        fi
        ;;
      409)
        info "device $REPORTER_HOSTNAME already exists; leaving as-is"
        ;;
      *)
        warn "device-create returned HTTP $_device_code: $(head -c 200 "$_device_body" 2>/dev/null)"
        warn "  Skipping self-bootstrap. Commission later via chat."
        ;;
    esac
  fi
  rm -f "$_cookies" "$_login_body" "$_device_body"
fi

echo
echo "✓ install complete"
echo
if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  echo "  Drift web UI:  $PUBLIC_URL"
  echo "  vmalert UI:    $PUBLIC_URL/vmalert/   (login as $WEB_AUTH_USER · the vmalert/AM UI password)"
  echo "  Alertmanager:  $PUBLIC_URL/am/        (login as $WEB_AUTH_USER · same password)"
  echo "  grafana:       $PUBLIC_URL/grafana/   (own auth — see grafana docs)"
  echo "  vmauth gateway: $PUBLIC_URL/vm/       (basic_auth reporter:$REPORTER_PASSWORD)"
  echo
  echo "First-run notes:"
  echo "  - DNS must resolve $DOMAIN → this host's IP before TLS can issue."
  echo "  - Watch issuance progress: docker compose logs -f caddy"
else
  echo "  Services bound to 127.0.0.1 — wire your existing reverse proxy:"
  echo "    Drift web UI:     127.0.0.1:${DRIFT_HOST_PORT:-10001}        →  $PUBLIC_URL/"
  echo "    vmalert:          127.0.0.1:${VMALERT_HOST_PORT:-8880}/vmalert  →  $PUBLIC_URL/vmalert/"
  echo "    alertmanager:     127.0.0.1:${ALERTMANAGER_HOST_PORT:-9093}/am  →  $PUBLIC_URL/am/"
  echo "    grafana:          127.0.0.1:${GRAFANA_HOST_PORT:-3000}     →  $PUBLIC_URL/grafana/"
  echo "    vmauth (writes):  127.0.0.1:${VMAUTH_HOST_PORT:-8427}      →  $PUBLIC_URL/vm/ + /vl/"
  echo
  echo "  Sample reverse-proxy configs (paste into your existing setup):"
  echo "    Caddy:    $DEPLOY_DIR/config/Caddyfile.sample"
  echo "    nginx:    $DEPLOY_DIR/config/nginx.conf.sample"
  echo "    Traefik:  $DEPLOY_DIR/config/traefik.yml.sample"
  echo "  None include basic_auth on /vmalert and /am — see each file's"
  echo "  header comment for how to add one if desired."
fi
echo
echo "  ntfy: subscribe to https://ntfy.sh/$NTFY_TOPIC on your phone"
echo
echo "  Apps + devices"
echo "    • This CP host is already self-scraped — reporter (vmagent +"
echo "      cadvisor + vector + node-exporter + process-exporter) is built"
echo "      into the bundle. Do NOT deploy the 'reporter' app on this"
echo "      server; the bundled reporter-* containers do the same job."
echo "    • The 'reporter' app is preloaded and ready to deploy to your"
echo "      other devices (Pis, edge boxes, fleet nodes) so their metrics"
echo "      and container logs flow back here. Commission a device via"
echo "      chat ('add device <name> to group <group>') and then deploy"
echo "      reporter to it."
echo "    • Drift Deploy also runs additional docker-compose-based apps"
echo "      on managed devices. Ship a compose bundle (own service, your"
echo "      own image), assign it to one device or a whole group, and the"
echo "      edge-agent applies + monitors it. Apps tab in the web UI."
echo

# Print every auto-generated secret in one block — the operator needs
# to save these somewhere (password manager). They also live in .env
# (mode 600) so this is the convenience copy, not the only copy.
if [ ${#GENERATED_SECRETS[@]} -gt 0 ]; then
  echo "════════════════════════════════════════════════════════════════════"
  echo "  Auto-generated credentials — save these now:"
  for s in "${GENERATED_SECRETS[@]}"; do
    echo "    $s"
  done
  echo "  (Also written to $ENV_FILE, mode 600.)"
  echo "════════════════════════════════════════════════════════════════════"
fi

