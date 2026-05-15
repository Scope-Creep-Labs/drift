from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str
    model: str = "claude-opus-4-7"
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

    @property
    def deploy_enabled(self) -> bool:
        """Deploy subsystem requires both Postgres and B2 storage."""
        return bool(self.drift_pg_url) and bool(self.b2_bucket)

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
