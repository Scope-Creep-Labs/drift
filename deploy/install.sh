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

# ---------- helpers ----------

err() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "warn:  $*" >&2; }
info() { echo "       $*"; }
heading() { echo; echo "═══ $* ═══"; }

# Read a single value from the existing .env (if any). Empty string if
# unset / missing. We use this so re-runs keep prior choices.
env_get() {
  local key=$1
  [ -f "$ENV_FILE" ] || { echo ""; return; }
  # Grep the assignment; allow leading whitespace; strip 'KEY=' prefix.
  local line
  line=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 || true)
  [ -z "$line" ] && { echo ""; return; }
  echo "${line#${key}=}"
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
}

# Secret-or-autogen: prompt for a password but generate a random one
# if the operator hits Enter and nothing's stored. Tracks generated
# values in $GENERATED_SECRETS for the "save these" exit summary.
GENERATED_SECRETS=()
ask_secret_autogen() {
  local key=$1 prompt=$2 length=${3:-20}
  local current
  current=$(env_get "$key")
  local hint=""
  if [ -n "$current" ]; then
    hint=" [Enter to keep current]"
  else
    hint=" [Enter to auto-generate]"
  fi
  local answer
  read -rsp "$prompt$hint: " answer
  echo
  if [ -n "$answer" ]; then
    eval "$key=\"\$answer\""
  elif [ -n "$current" ]; then
    eval "$key=\"\$current\""
  else
    local generated
    generated=$(rand_token "$length")
    eval "$key=\"\$generated\""
    # Stash for the exit summary so the operator can save it.
    GENERATED_SECRETS+=("$key=$generated")
  fi
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

[ -f "$ENV_FILE" ] && info "found existing .env (will prompt to keep or change values)"

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
  ask DRIFT_HOST_PORT "Local port to bind drift-frontend on (127.0.0.1:<port>)" 10001
fi

# Web-auth gate for /vmalert/ and /am/ — needed in BOTH modes (ntfy
# notifications link straight to vmalert and Alertmanager has no native
# auth). Auto-generated by default; press Enter to accept.
heading "Web-auth gate (vmalert + Alertmanager UIs)"
ask WEB_AUTH_USER "Username for the /vmalert and /am basic-auth gate" drift
ask_secret_autogen WEB_AUTH_PASSWORD_PLAINTEXT "Password for the same gate"

heading "Drift admin"
ask DRIFT_ADMIN_USERNAME "Drift admin username" admin
ask_secret_autogen DRIFT_ADMIN_PASSWORD "Drift admin password"

heading "LLM"
echo "  Pick the model Drift's agent will run. The matching API key is asked next."
echo "  Common picks: claude-opus-4-7 | gpt-5.4-mini | gpt-4o | o3 | gemini-2.5-pro"
ask MODEL "Model id" claude-opus-4-7
ask EFFORT "Reasoning effort (low/medium/high)" medium
ask MAX_TOKENS "Max output tokens per call" 64000
# Only prompt for the key that matches the chosen model's provider.
case "$MODEL" in
  claude-*|*/claude-*) ask_secret ANTHROPIC_API_KEY "Anthropic API key" ;;
  gpt-*|o1*|o3*|*/gpt-*|*/o1*|*/o3*) ask_secret OPENAI_API_KEY "OpenAI API key" ;;
  gemini-*|*/gemini-*) ask_secret GEMINI_API_KEY "Gemini API key" ;;
  *) warn "Unknown model prefix '$MODEL' — set the right *_API_KEY in .env manually after install." ;;
esac

heading "ntfy push (Alertmanager → phone)"
echo "  Pick any unique-ish topic; subscribe to https://ntfy.sh/<topic> on your phone."
DEFAULT_NTFY="drift-$(rand_token 8)"
ask NTFY_TOPIC "ntfy topic" "$DEFAULT_NTFY"

heading "Bundle storage (for Drift Deploy compose bundles)"
echo "  Default 'local' stores bundles on this host's filesystem and"
echo "  serves them via the CP. Switch to 's3' if you want bundles in"
echo "  an external bucket (B2/AWS/MinIO) — useful for multi-CP setups."
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
fi

heading "Self-scrape (reporter on this host)"
ask REPORTER_HOSTNAME "Hostname label for self-scraped metrics" "$(hostname -s 2>/dev/null || echo drift-host)"
ask REPORTER_GROUP    "Group label for self-scraped metrics" cloud

heading "Auto-generated secrets"
# These get rolled fresh on each rotation but preserved when re-running
# without an explicit rotate request.
DRIFT_PG_PASSWORD=$(env_get DRIFT_PG_PASSWORD)
DRIFT_SECRET_KEY=$(env_get DRIFT_SECRET_KEY)
REPORTER_PASSWORD=$(env_get REPORTER_PASSWORD)
if [ -z "$DRIFT_PG_PASSWORD" ]; then
  DRIFT_PG_PASSWORD=$(rand_token 24)
  info "generated DRIFT_PG_PASSWORD"
else
  info "kept existing DRIFT_PG_PASSWORD"
fi
if [ -z "$DRIFT_SECRET_KEY" ]; then
  DRIFT_SECRET_KEY=$(gen_fernet)
  info "generated DRIFT_SECRET_KEY (Fernet)"
else
  info "kept existing DRIFT_SECRET_KEY"
fi
if [ -z "$REPORTER_PASSWORD" ]; then
  REPORTER_PASSWORD=$(rand_token 24)
  info "generated REPORTER_PASSWORD (for remote vmagents)"
  GENERATED_SECRETS+=("REPORTER_PASSWORD=$REPORTER_PASSWORD")
else
  info "kept existing REPORTER_PASSWORD"
fi

heading "Hashing web-auth password (bcrypt via caddy:2)"
WEB_AUTH_HASH=$(bcrypt_caddy "$WEB_AUTH_PASSWORD_PLAINTEXT")
info "bcrypt hash generated"
# Compose interpolates `$X` syntax inside .env values. Bcrypt hashes
# start with `$2a$14$...` which compose would otherwise read as three
# variable references. Double the dollars so compose treats them
# literally — caddy still sees the original hash because compose
# un-escapes `$$` → `$` when it injects the value into the container's
# env. Same trick docker-compose.yml uses for $$ in command args.
WEB_AUTH_HASH_ENV=${WEB_AUTH_HASH//$/$$}

# ---------- write .env ----------

heading "Writing .env"
umask 077
cat > "$ENV_FILE" <<EOF
# Generated by install.sh — re-run install.sh to update.
USE_BUNDLED_CADDY=$USE_BUNDLED_CADDY
DOMAIN=$DOMAIN
LETSENCRYPT_EMAIL=$LETSENCRYPT_EMAIL
PUBLIC_URL=$PUBLIC_URL
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
chmod 600 "$ENV_FILE"
info ".env written ($(wc -l < "$ENV_FILE") lines, mode 600)"

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
  # External reverse-proxy mode. Render a sample site block the
  # operator can paste into their existing Caddyfile (or translate
  # to nginx/Traefik). Uses 127.0.0.1:<port> upstreams that match
  # the loopback bindings in docker-compose.yml.
  render config/Caddyfile.external.tmpl     config/Caddyfile.sample \
    __DOMAIN__                "$DOMAIN" \
    __DRIFT_HOST_PORT__       "${DRIFT_HOST_PORT:-10001}" \
    __VMALERT_HOST_PORT__     "${VMALERT_HOST_PORT:-8880}" \
    __ALERTMANAGER_HOST_PORT__ "${ALERTMANAGER_HOST_PORT:-9093}" \
    __GRAFANA_HOST_PORT__     "${GRAFANA_HOST_PORT:-3000}" \
    __VMAUTH_HOST_PORT__      "${VMAUTH_HOST_PORT:-8427}" \
    __WEB_AUTH_USER__         "$WEB_AUTH_USER" \
    __WEB_AUTH_HASH__         "$WEB_AUTH_HASH"
  info "  config/Caddyfile.sample written — paste into your existing reverse-proxy config"
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
if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  COMPOSE_ARGS=(--profile caddy)
else
  COMPOSE_ARGS=()
fi
docker compose "${COMPOSE_ARGS[@]}" pull
docker compose "${COMPOSE_ARGS[@]}" up -d
echo
heading "Status"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo
echo "✓ install complete"
echo
if [ "$USE_BUNDLED_CADDY" = "true" ]; then
  echo "  Drift web UI:  $PUBLIC_URL"
  echo "  vmalert:       $PUBLIC_URL/vmalert/   (basic_auth $WEB_AUTH_USER:…)"
  echo "  alertmanager:  $PUBLIC_URL/am/        (basic_auth $WEB_AUTH_USER:…)"
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
  echo "  Sample Caddyfile for the above is at:"
  echo "    $DEPLOY_DIR/config/Caddyfile.sample"
  echo "  (Translate to nginx/Traefik if you don't use Caddy.)"
fi
echo
echo "  ntfy: subscribe to https://ntfy.sh/$NTFY_TOPIC on your phone"
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
