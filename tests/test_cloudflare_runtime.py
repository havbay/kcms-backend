from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import kcms.cloudflare_runtime as cloudflare_runtime
from kcms.app import app, lifespan
from kcms.cloudflare_runtime import environment_from_bindings


def test_environment_from_bindings_maps_runtime_values_and_hyperdrive() -> None:
    env = SimpleNamespace(
        CORS_ORIGINS="https://findmoy.app",
        PUBLIC_FRONTEND_URL="https://findmoy.app",
        SENTRY_ENVIRONMENT="production",
        HYPERDRIVE=SimpleNamespace(
            host="hyperdrive.example",
            port="5432",
            user="kcms user",
            password="secret/value",
            database="kcms production",
        ),
    )

    values = environment_from_bindings(env)

    assert values["CORS_ORIGINS"] == "https://findmoy.app"
    assert values["PUBLIC_FRONTEND_URL"] == "https://findmoy.app"
    assert values["SENTRY_ENVIRONMENT"] == "production"
    assert values["DATABASE_URL"] == (
        "postgresql://kcms%20user:secret%2Fvalue@hyperdrive.example:5432/"
        "kcms%20production?sslmode=disable"
    )


def test_environment_from_bindings_ignores_absent_optional_values() -> None:
    values = environment_from_bindings(SimpleNamespace(CORS_ORIGINS="https://findmoy.app"))

    assert values == {"CORS_ORIGINS": "https://findmoy.app"}


def test_environment_from_bindings_can_defer_hyperdrive_access() -> None:
    class RuntimeEnvironment:
        CORS_ORIGINS = "https://findmoy.app"

        @property
        def HYPERDRIVE(self):
            raise AssertionError("Hyperdrive must only be read inside an event handler")

    values = environment_from_bindings(
        RuntimeEnvironment(),
        include_hyperdrive=False,
    )

    assert values == {"CORS_ORIGINS": "https://findmoy.app"}


@pytest.mark.asyncio
async def test_cloudflare_lifespan_skips_process_owned_startup_jobs(monkeypatch) -> None:
    calls: list[str] = []

    async def connect(*args, **kwargs) -> None:
        calls.append("connect")

    async def disconnect() -> None:
        calls.append("disconnect")

    @asynccontextmanager
    async def unexpected_acquire():
        calls.append("acquire")
        raise AssertionError("Cloudflare startup must not apply migrations")
        yield

    async def unexpected_sweep(*args, **kwargs) -> None:
        raise AssertionError("Cloudflare startup must not run a forever loop")

    monkeypatch.setattr(
        "kcms.app.settings",
        SimpleNamespace(
            database_url="postgresql://example/db",
            database_connect_timeout_seconds=3,
            run_migrations_on_startup=False,
            run_quarantine_sweep=False,
            quarantine_sweep_interval_seconds=30,
        ),
    )
    monkeypatch.setattr("kcms.app.database.connect", connect)
    monkeypatch.setattr("kcms.app.database.disconnect", disconnect)
    monkeypatch.setattr("kcms.app.database.acquire", unexpected_acquire)
    monkeypatch.setattr("kcms.app.run_quarantine_sweep", unexpected_sweep)

    async with lifespan(app):
        pass

    assert calls == ["connect", "disconnect"]


@pytest.mark.asyncio
async def test_scheduled_sweep_owns_its_database_lifecycle() -> None:
    calls: list[object] = []

    class Database:
        async def connect(self, dsn: str, *, timeout_seconds: float) -> None:
            calls.extend(("connect", dsn, timeout_seconds))

        async def disconnect(self) -> None:
            calls.append("disconnect")

    async def sweep() -> int:
        calls.append("sweep")
        return 2

    runner = getattr(cloudflare_runtime, "run_scheduled_sweep", None)
    assert runner is not None

    deleted = await runner(
        Database(),
        SimpleNamespace(
            database_url="postgresql://example/db",
            database_connect_timeout_seconds=3,
        ),
        sweep,
    )

    assert deleted == 2
    assert calls == ["connect", "postgresql://example/db", 3, "sweep", "disconnect"]
