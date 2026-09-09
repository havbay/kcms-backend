"""Platform Administration read models and privacy boundary."""

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.settings import settings
from kcms.shared.database import database


async def signed_up_client(app, email: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "a-long-enough-password",
            "display_name": "Admin Test User",
        },
    )
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"
    return client


@pytest.fixture
async def app(monkeypatch):
    monkeypatch.setattr(settings, "platform_admin_emails", "")
    application = create_app()
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        yield application


async def test_platform_admin_dashboard_is_denied_to_clients(app):
    client = await signed_up_client(app, f"client-{uuid.uuid4().hex[:8]}@example.com")
    try:
        for path in (
            "/api/v1/admin/overview",
            "/api/v1/admin/workspaces",
            "/api/v1/admin/workspaces/missing",
            "/api/v1/admin/integrations",
            "/api/v1/admin/plans",
            "/api/v1/admin/access",
            "/api/v1/admin/audit-log",
        ):
            assert (await client.get(path)).status_code == 403
    finally:
        await client.aclose()


async def test_platform_admin_can_read_workspace_operations_without_comment_content(
    app, monkeypatch
):
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(settings, "platform_admin_emails", email)
    admin = await signed_up_client(app, email)
    try:
        overview = await admin.get("/api/v1/admin/overview")
        assert overview.status_code == 200, overview.text
        overview_body = overview.json()
        assert overview_body["total_workspaces"] >= 1
        assert overview_body["recent_workspaces"]

        workspace_id = overview_body["recent_workspaces"][0]["id"]
        detail = await admin.get(f"/api/v1/admin/workspaces/{workspace_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["id"] == workspace_id
        assert "members" in body and "pages" in body and "metrics" in body
        assert "text" not in str(body)
        assert "credential_ciphertext" not in str(body)
    finally:
        await admin.aclose()


async def test_platform_admin_can_manage_entitlements_and_review_audit(app, monkeypatch):
    email = f"controls-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(settings, "platform_admin_emails", email)
    admin = await signed_up_client(app, email)
    try:
        plans = await admin.get("/api/v1/admin/plans")
        assert plans.status_code == 200, plans.text
        assert {item["plan"] for item in plans.json()["plans"]} == {"TRIAL", "STARTER", "GROWTH"}

        integrations = await admin.get("/api/v1/admin/integrations")
        assert integrations.status_code == 200, integrations.text
        assert "credential_ciphertext" not in integrations.text

        access = await admin.get("/api/v1/admin/access")
        assert access.status_code == 200, access.text
        assert any(item["email"] == email for item in access.json()["admins"])

        overview = await admin.get("/api/v1/admin/overview")
        workspace_id = overview.json()["recent_workspaces"][0]["id"]
        updated = await admin.patch(
            f"/api/v1/admin/workspaces/{workspace_id}/entitlement",
            json={"suspended": True},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "SUSPENDED"

        audit = await admin.get("/api/v1/admin/audit-log")
        assert audit.status_code == 200, audit.text
        assert any(
            event["action"] == "UPDATE_WORKSPACE_ENTITLEMENT"
            and event["target_id"] == workspace_id
            for event in audit.json()
        )
    finally:
        await admin.aclose()
