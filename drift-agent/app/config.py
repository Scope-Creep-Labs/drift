from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM routing goes through litellm — `model` is a litellm model id.
    # Examples: claude-opus-4-7, gpt-4o, gpt-4o-mini, o1, o3,
    # gemini-2.5-pro. Provider is inferred from the prefix and the
    # corresponding *_API_KEY is used. Only the key for the chosen
    # provider needs to be set; the others stay empty.
    model: str = "claude-opus-4-7"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    # Local-model endpoint. When MODEL starts with ollama/ or ollama_chat/,
    # LiteLLM uses this URL to reach the operator's Ollama daemon. The
    # installer prompts for it with the host.docker.internal default, so
    # the drift-agent container reaches a daemon running on the host.
    ollama_api_base: str = ""
    effort: str = "high"
    max_tokens: int = 64000

    vm_url: str = ""
    vm_tenant_path: str = ""
    vm_basic_auth: str = ""
    vm_bearer_token: str = ""

    # vmalert + Alertmanager. Both are optional — if URLs are empty, the
    # alert tools just refuse to run with a clear error message.
    vmalert_url: str = ""
    vmalert_basic_auth: str = ""
    alertmanager_url: str = ""
    alertmanager_basic_auth: str = ""

    # VictoriaLogs (logs subsystem). Same shape as VM — optional, basic-auth
    # credentials are usually the same vmauth `reporter` user since logs go
    # through the same gateway. Leave VL_URL empty to disable the log tools.
    vl_url: str = ""
    vl_basic_auth: str = ""

    # Path (inside the drift-agent container) where rule files live. The
    # agent reads/writes only `<dir>/drift-managed.yml` to avoid touching
    # hand-edited rule files in the same directory.
    vmalert_rules_dir: str = "/etc/alerts"

    # alertmanager.yml location inside the drift-agent container (rw mount)
    # and the secrets directory (ro mount). Receiver secrets are referenced
    # as `<secrets_dir>/<filename>` from the agent's writes; the agent never
    # reads file contents — just checks for presence.
    alertmanager_config_file: str = "/etc/alertmanager/alertmanager.yml"
    alertmanager_secrets_dir: str = "/etc/alertmanager/secrets"

    # Drift Deploy — control plane state + bundle storage. All optional; an
    # empty drift_pg_url makes the deploy subsystem dormant.
    drift_pg_url: str = ""

    # Bundle storage: "local" = CP-hosted filesystem (zero external deps;
    # the drift-agent serves bundles via /api/deploy/agent/bundles/...).
    # "s3" = upload to B2/AWS/MinIO; agents download via presigned URLs.
    # Default is "local" because most self-hosters don't want an extra
    # cloud bucket just to push compose bundles.
    bundle_storage: str = "local"
    bundle_storage_path: str = "/var/lib/drift/bundles"

    # B2 / S3 — only consulted when bundle_storage=s3.
    b2_endpoint: str = ""
    b2_region: str = ""
    b2_access_key_id: str = ""
    b2_secret_access_key: str = ""
    b2_bucket: str = ""
    b2_prefix: str = "drift-bundles"

    # Fernet symmetric key (urlsafe base64, 44 chars). Required for the
    # secrets subsystem — registry credentials, future env-var secrets.
    # Generate once with: python -c "from cryptography.fernet import
    # Fernet; print(Fernet.generate_key().decode())". Empty value disables
    # the secrets endpoints (they return 503).
    drift_secret_key: str = ""

    # Bootstrap admin user — created/updated on every startup. The first
    # operator account; required at least once so the system isn't
    # locked out. Subsequent admins managed via the user CRUD endpoints.
    drift_admin_username: str = ""
    drift_admin_password: str = ""

    # CP-side facts surfaced to edge devices on every check-in (see
    # AgentCheckInResponse.cp_public_url / vm_write_password). The
    # edge agent persists them to /etc/drift-deploy/env and exports
    # them as DRIFT_CP_PUBLIC_URL / DRIFT_VM_WRITE_USER /
    # DRIFT_VM_WRITE_PASSWORD into compose subshells, so reporter-style
    # bundles can reference the CP without baking the URL/password in.
    public_url: str = ""
    reporter_password: str = ""

    # Telegram bot (v0.1.66+). Two surfaces: chat with the agent over
    # long polling, and an Alertmanager → Telegram bridge. Both opt-in
    # via TELEGRAM_BOT_TOKEN; with it unset, the router still mounts but
    # every endpoint returns 503 and the bot loop never starts.
    telegram_bot_token: str = ""
    # Long-poll timeout per getUpdates call (Telegram cap is 50, recommended
    # 25-50; the client adds a 10s grace so the connection isn't cut).
    telegram_poll_timeout: int = 30
    # TTL on a /link code before it's rejected.
    telegram_link_code_ttl_min: int = 10
    # Comma-separated chat IDs the Alertmanager bridge fans alerts out to.
    # Empty = alerts disabled even with a bot token configured (chat side
    # still works). Whitespace tolerated; non-digit entries dropped.
    telegram_alert_chats: str = ""
    # Shared secret in the Alertmanager webhook URL. Empty = webhook is
    # closed even if the bot is otherwise live (returns 404). Generate
    # something opaque, e.g. python -c "import secrets; print(secrets.token_urlsafe(24))".
    telegram_webhook_secret: str = ""

    @property
    def telegram_alert_chats_list(self) -> list[str]:
        return [
            x.strip()
            for x in (self.telegram_alert_chats or "").split(",")
            if x.strip()
        ]

    # Base domain for tunnel subdomains. When set, the tunnel feature mints
    # sessions whose URLs look like `tunnel-<token>.<tunnel_base_domain>`;
    # the subdomain router matches incoming requests by `Host` header.
    # Leave empty to disable the tunnel feature (POST /tunnel/open returns
    # 503). Should NOT include a scheme — just the bare domain (e.g.
    # "dabba.princesamuel.me"). Operators also need a wildcard A record
    # pointing `*.<tunnel_base_domain>` at the CP host + a Caddy site
    # block with on-demand TLS for `tunnel-*.<tunnel_base_domain>` — see
    # the release notes for the full setup.
    tunnel_base_domain: str = ""
    # How long a freshly minted tunnel is valid for (idle or active —
    # there's no in-use timer extension). Operators reopen after expiry.
    tunnel_default_ttl_seconds: int = 4 * 60 * 60  # 4h
    # CP-wide ceiling so a runaway script can't open thousands of tunnels
    # and exhaust the pg connection pool / OS fd budget.
    tunnel_max_concurrent: int = 32

    # Tarball release this stack was installed from. Stamped into .env
    # by install.sh (which itself is stamped by package-release.sh at
    # tarball-build time). Empty / "dev" for unpackaged installs.
    install_version: str = ""

    # When true, session cookies are sent without Secure so plain-http
    # localhost dev works. Production should leave this false (default)
    # so cookies require HTTPS.
    dev_mode: bool = False

    # When set, `Set-Cookie` on login uses Domain=<value> so the session
    # cookie is sent to every subdomain of that domain — required for the
    # tunnel feature, where `tunnel-<token>.<base>` needs to read the
    # caller's drift_session to authorize the proxy. Leave empty for the
    # default behavior (host-scoped — cookie only goes to the exact host
    # where login happened). install.sh sets this to $DOMAIN on fresh
    # installs; existing logins keep working but won't reach tunnel
    # subdomains until the user logs out + back in once.
    session_cookie_domain: str = ""

    # Device-freshness reaper: if a device's last_seen is older than this
    # many seconds AND its status is still "online", flip it to "offline"
    # on the next observability-refresh tick. 300s = 5 min, ~20 missed
    # 15-second poll cycles — slack enough to not flap on transient
    # blips, tight enough to surface real outages within minutes.
    drift_device_stale_after_seconds: int = 300

    # Login rate limiting (in-memory, per drift-agent process). Failed
    # password checks at /api/auth/login and /api/auth/me/password are
    # tracked in two sliding windows: per username (catches slow
    # account-grinding) and per source IP (catches credential stuffing
    # across many accounts from one source). Either bucket hitting its
    # max returns 429 and skips bcrypt verify entirely. Successful login
    # clears the username bucket; the IP bucket is never cleared by
    # success, so a single correct guess can't reset network-wide
    # enforcement. Tune higher for shared-IP environments (office NAT,
    # CGNAT residential), lower for single-user installs.
    login_max_failures_per_username: int = 5
    login_max_failures_per_ip: int = 30
    login_failure_window_seconds: int = 900  # 15 minutes

    # Demo mode. When enabled, the CP refuses operator-side mutations
    # that would either compromise the shared demo account's experience
    # (changing the admin password / LLM key / API key for everyone) or
    # corrupt the simulator's fixed device fleet (commission / delete
    # device). Per-session investigation turns are capped so a single
    # demo visitor can't burn the LLM budget. The frontend reads
    # /api/auth/me to know whether to show the demo banner and hide
    # admin-only affordances.
    #
    # Default off — drift behaves identically to non-demo when these
    # are unset.
    demo_mode: bool = False
    demo_max_turns_per_session: int = 10
    demo_banner_message: str = (
        "Demo mode — actions are visible to other operators sharing this account. "
        "State resets nightly."
    )

    @property
    def deploy_enabled(self) -> bool:
        """Deploy subsystem requires Postgres + a bundle storage backend.
        Local-fs storage (BUNDLE_STORAGE=local) is now the default — the
        old check required a B2 bucket, which silently disabled all
        deploy tools on a fresh single-server install."""
        if not self.drift_pg_url:
            return False
        if self.bundle_storage == "local":
            return True
        return bool(self.b2_bucket)

    @property
    def secrets_enabled(self) -> bool:
        """Secrets subsystem additionally requires an encryption key."""
        return self.deploy_enabled and bool(self.drift_secret_key)

    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def vm_base(self) -> str:
        url = self.vm_url.rstrip("/")
        if self.vm_tenant_path:
            url += "/" + self.vm_tenant_path.strip("/")
        return url

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]
