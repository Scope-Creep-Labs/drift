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

    # When true, session cookies are sent without Secure so plain-http
    # localhost dev works. Production should leave this false (default)
    # so cookies require HTTPS.
    dev_mode: bool = False

    # Device-freshness reaper: if a device's last_seen is older than this
    # many seconds AND its status is still "online", flip it to "offline"
    # on the next observability-refresh tick. 300s = 5 min, ~20 missed
    # 15-second poll cycles — slack enough to not flap on transient
    # blips, tight enough to surface real outages within minutes.
    drift_device_stale_after_seconds: int = 300

    @property
    def deploy_enabled(self) -> bool:
        """Deploy subsystem requires both Postgres and B2 storage."""
        return bool(self.drift_pg_url) and bool(self.b2_bucket)

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
