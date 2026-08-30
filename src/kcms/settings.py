from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings. No credentials are committed."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://kcms:kcms@127.0.0.1:5432/kcms"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    contract_version: str = "1.0.0"
    # Telegram sign-in stays disabled until a bot token is configured. It must
    # fail closed: an unconfigured provider is unavailable, never open.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
