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
