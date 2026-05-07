from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str
    model: str = "claude-opus-4-7"
    effort: str = "high"
    max_tokens: int = 64000

    vm_url: str = "http://victoriametrics:8428"
    vm_tenant_path: str = ""
    vm_basic_auth: str = ""
    vm_bearer_token: str = ""

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
