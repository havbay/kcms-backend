"""Drives the system through HTTP, asserting only what a client could observe."""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.shared.database import database


@pytest.fixture
async def client(monkeypatch):
    app = create_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_health_reports_ready_when_database_answers(client, monkeypatch):
    async def reachable() -> bool:
        return True

    monkeypatch.setattr(database, "is_reachable", reachable)
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "kcms-backend",
        "status": "READY",
        "database": "REACHABLE",
        "contract_version": "1.0.0",
    }


async def test_health_reports_degraded_without_leaking_detail(client, monkeypatch):
    async def unreachable() -> bool:
        return False

    monkeypatch.setattr(database, "is_reachable", unreachable)
    response = await client.get("/api/v1/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "DEGRADED"
    assert body["database"] == "UNREACHABLE"
    # The contract forbids exception or connection detail.
    assert set(body) == {"service", "status", "database", "contract_version"}


async def test_probe_failure_is_swallowed_and_reported_as_unreachable():
    await database.disconnect()
    assert await database.is_reachable() is False
