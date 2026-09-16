"""Translate Cloudflare Worker bindings into the app's settings interface."""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

SETTING_NAMES = (
    "AUTO_REMOVAL_ENABLED",
    "CLERK_JWT_ISSUER",
    "CLERK_SECRET_KEY",
    "CONTRACT_VERSION",
    "CORS_ORIGINS",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "DATABASE_PER_REQUEST_CONNECTIONS",
    "DATABASE_URL",
    "INTEGRATION_ENCRYPTION_KEY",
    "META_APP_ID",
    "META_APP_SECRET",
    "META_GRAPH_VERSION",
    "META_LOGIN_CONFIG_ID",
    "META_OAUTH_REDIRECT_URI",
    "META_OAUTH_SCOPES",
    "PLATFORM_ADMIN_EMAILS",
    "PUBLIC_FRONTEND_URL",
    "PUBLIC_SIGNUP_ENABLED",
    "QUARANTINE_SWEEP_INTERVAL_SECONDS",
    "RUN_MIGRATIONS_ON_STARTUP",
    "RUN_QUARANTINE_SWEEP",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "SMTP_FROM_EMAIL",
    "SMTP_FROM_NAME",
    "SMTP_HOST",
    "SMTP_PASSWORD",
    "SMTP_PORT",
    "SMTP_TIMEOUT_SECONDS",
    "SMTP_USERNAME",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_USERNAME",
)


def _binding(env: object, name: str) -> object | None:
    try:
        return getattr(env, name)
    except (AttributeError, KeyError):
        return None


def database_url_from_hyperdrive(binding: Any) -> str:
    user = quote(str(binding.user), safe="")
    password = quote(str(binding.password), safe="")
    host = str(binding.host)
    port = str(binding.port)
    database = quote(str(binding.database), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}?sslmode=disable"


def environment_from_bindings(
    env: object,
    *,
    include_hyperdrive: bool = True,
) -> dict[str, str]:
    values = {
        name: str(value)
        for name in SETTING_NAMES
        if (value := _binding(env, name)) is not None
    }
    if include_hyperdrive and (hyperdrive := _binding(env, "HYPERDRIVE")) is not None:
        values["DATABASE_URL"] = database_url_from_hyperdrive(hyperdrive)
    return values


async def run_scheduled_sweep(
    database_owner: Any,
    runtime_settings: Any,
    sweep: Callable[[], Awaitable[int]],
) -> int:
    await database_owner.connect(
        runtime_settings.database_url,
        timeout_seconds=runtime_settings.database_connect_timeout_seconds,
    )
    try:
        return await sweep()
    finally:
        await database_owner.disconnect()
