import asyncio
import time

import pytest

from kcms.shared.database.pool import Database


@pytest.mark.asyncio
async def test_connect_applies_the_configured_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def create_pool(dsn: str, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("kcms.shared.database.pool.asyncpg.create_pool", create_pool)
    database = Database()

    await database.connect("postgresql://example/db", timeout_seconds=3)

    assert captured == {
        "dsn": "postgresql://example/db",
        "min_size": 1,
        "max_size": 5,
        "timeout": 3,
    }


@pytest.mark.asyncio
async def test_connect_enforces_timeout_when_driver_does_not_return(monkeypatch) -> None:
    async def create_pool(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("kcms.shared.database.pool.asyncpg.create_pool", create_pool)
    database = Database()
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            database.connect("postgresql://example/db", timeout_seconds=0.01),
            timeout=0.1,
        )

    assert time.monotonic() - started < 0.05


@pytest.mark.asyncio
async def test_pool_stays_open_until_every_lifespan_disconnects(monkeypatch) -> None:
    close_calls = 0
    create_calls = 0

    class Pool:
        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    async def create_pool(*args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        return Pool()

    monkeypatch.setattr("kcms.shared.database.pool.asyncpg.create_pool", create_pool)
    database = Database()

    await database.connect("postgresql://example/db")
    await database.connect("postgresql://example/db")
    await database.disconnect()

    assert create_calls == 1
    assert close_calls == 0
    assert database.connected

    await database.disconnect()

    assert close_calls == 1
    assert not database.connected
