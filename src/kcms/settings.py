from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings. No credentials are committed."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://kcms:kcms@127.0.0.1:5432/kcms"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    contract_version: str = "1.0.0"
    sentry_dsn: str = ""
    sentry_environment: str = "production"
    clerk_jwt_issuer: str = ""
    # Client accounts are created through reviewed, one-time setup invitations.
    # Tests may enable direct signup explicitly to create isolated fixtures.
    public_signup_enabled: bool = False
    # Telegram sign-in stays disabled until a bot token is configured. It must
    # fail closed: an unconfigured provider is unavailable, never open.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    # Comma-separated emails granted Platform Administration at sign-in. The
    # role is never settable through the API, so it cannot be self-assigned.
    platform_admin_emails: str = ""
    public_frontend_url: str = "http://127.0.0.1:5173"
    # Transactional email is optional. With any required value absent, the
    # backend keeps onboarding functional through an audited manual-link path.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "KCMS"
    smtp_timeout_seconds: int = 15
    # Provider configuration. The Graph version is explicit because Meta API
    # versions age out; an empty value keeps the integration unavailable rather
    # than silently choosing a version at runtime.
    meta_graph_version: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_login_config_id: str = ""
    meta_oauth_redirect_uri: str = ""
    meta_oauth_scopes: str = (
        "pages_show_list,pages_read_engagement,pages_read_user_content,"
        "pages_manage_engagement,pages_manage_metadata"
    )
    integration_encryption_key: str = ""
    # How often the quarantine sweep checks for HARMFUL comments whose
    # auto-delete delay has expired. Short enough to keep the countdown badge
    # honest, long enough not to hammer the Graph API.
    quarantine_sweep_interval_seconds: int = 30
    # Removing a comment without asking a person is off while the classifier is
    # rule-based. A keyword list is not evidence enough to destroy a customer's
    # comment irreversibly, and D-010 still holds: every Page action is a human
    # decision. The routing that decides what *would* be auto-removed is kept
    # and tested, so this is one value to change when a trained model earns it.
    auto_removal_enabled: bool = False

    @property
    def smtp_configured(self) -> bool:
        return all(
            (
                self.smtp_host,
                self.smtp_username,
                self.smtp_password,
                self.smtp_from_email,
            )
        )

    @property
    def platform_admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.platform_admin_emails.split(",") if e.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
