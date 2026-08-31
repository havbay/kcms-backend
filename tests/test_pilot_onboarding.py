import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.api.pilot_requests import get_notification_sender
from kcms.app import create_app
from kcms.notifications.contracts import DeliveryResult
from kcms.settings import settings
from kcms.shared.database import database


class FakeSender:
    def __init__(self, status: str = "SENT"):
        self.status = status
        self.notifications = []

    async def send(self, notification):
        self.notifications.append(notification)
        return DeliveryResult(self.status, "fake")


async def sign_up(app, email: str, *, admin: bool = False) -> httpx.AsyncClient:
    if admin:
        settings.platform_admin_emails = email
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    created = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "a-long-enough-password",
            "display_name": "Platform Admin" if admin else "Existing Client",
            "organization": "KCMS" if admin else "Existing Shop",
        },
    )
    assert created.status_code == 201, created.text
    client.headers["Authorization"] = f"Bearer {created.json()['token']}"
    return client


@pytest.fixture
async def app(monkeypatch):
    monkeypatch.setattr(settings, "platform_admin_emails", "")
    monkeypatch.setattr(settings, "public_frontend_url", "https://kcms.example")
    application = create_app()
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        yield application


def request_body(email: str) -> dict[str, str]:
    return {
        "name": "Dara Sok",
        "organization": "Angkor Shop",
        "email": email,
        "facebook_page": "facebook.com/angkorshop",
        "note": "We want help reviewing Khmer scam replies.",
    }


async def test_a_visitor_can_request_a_pilot_without_an_account(app):
    sender = FakeSender()
    app.dependency_overrides[get_notification_sender] = lambda: sender
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        response = await anonymous.post(
            "/api/v1/pilot-requests", json=request_body("pilot-new@example.com")
        )

    assert response.status_code == 202
    assert set(response.json()) == {"id", "status", "message"}
    assert response.json()["status"] == "PENDING"
    assert sender.notifications[0].kind == "PILOT_REQUEST_RECEIVED"


async def test_only_platform_admins_can_list_and_decide_pilot_requests(app):
    ordinary = await sign_up(app, f"ordinary-{uuid.uuid4().hex[:8]}@example.com")
    try:
        assert (await ordinary.get("/api/v1/admin/pilot-requests")).status_code == 403
        assert (
            await ordinary.post(
                "/api/v1/admin/pilot-requests/missing/decision",
                json={"decision": "APPROVED"},
            )
        ).status_code == 403
    finally:
        await ordinary.aclose()


async def test_approval_of_a_new_email_creates_one_time_setup_link_and_records_delivery(app):
    sender = FakeSender()
    app.dependency_overrides[get_notification_sender] = lambda: sender
    visitor = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    async with visitor:
        request_id = (
            await visitor.post("/api/v1/pilot-requests", json=request_body(email))
        ).json()["id"]

    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    admin = await sign_up(app, admin_email, admin=True)
    try:
        approved = await admin.post(
            f"/api/v1/admin/pilot-requests/{request_id}/decision",
            json={"decision": "APPROVED"},
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "APPROVED"
        assert body["delivery_status"] == "SENT"
        assert body["invitation_url"].startswith("https://kcms.example/setup/")
        assert sender.notifications[-1].kind == "PILOT_APPROVED"
        assert body["invitation_url"] in sender.notifications[-1].text
    finally:
        await admin.aclose()

    token = body["invitation_url"].rsplit("/", 1)[1]
    setup = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with setup:
        preview = await setup.get(f"/api/v1/setup-invitations/{token}")
        assert preview.status_code == 200
        assert preview.json()["organization"] == "Angkor Shop"
        completed = await setup.post(
            f"/api/v1/setup-invitations/{token}/accept",
            json={"display_name": "Dara Sok", "password": "a-new-secure-password"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["user"]["display_name"] == "Dara Sok"
        replay = await setup.post(
            f"/api/v1/setup-invitations/{token}/accept",
            json={"display_name": "Someone Else", "password": "another-password"},
        )
        assert replay.status_code == 409


async def test_smtp_failure_does_not_rollback_approval_and_returns_manual_link(app):
    sender = FakeSender("MANUAL_REQUIRED")
    app.dependency_overrides[get_notification_sender] = lambda: sender
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        request_id = (
            await anonymous.post("/api/v1/pilot-requests", json=request_body(email))
        ).json()["id"]

    admin = await sign_up(app, f"admin-{uuid.uuid4().hex[:8]}@example.com", admin=True)
    try:
        decision = await admin.post(
            f"/api/v1/admin/pilot-requests/{request_id}/decision",
            json={"decision": "APPROVED"},
        )
        assert decision.status_code == 200
        assert decision.json()["delivery_status"] == "MANUAL_REQUIRED"
        assert decision.json()["invitation_url"]
        rows = (await admin.get("/api/v1/admin/pilot-requests")).json()
        row = next(item for item in rows if item["id"] == request_id)
        assert row["delivery_status"] == "MANUAL_REQUIRED"
    finally:
        await admin.aclose()


async def test_existing_account_is_upgraded_without_emailing_a_password_or_new_setup_token(app):
    sender = FakeSender()
    app.dependency_overrides[get_notification_sender] = lambda: sender
    email = f"existing-{uuid.uuid4().hex[:8]}@example.com"
    existing = await sign_up(app, email)
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        request_id = (
            await anonymous.post("/api/v1/pilot-requests", json=request_body(email))
        ).json()["id"]
    admin = await sign_up(app, f"admin-{uuid.uuid4().hex[:8]}@example.com", admin=True)
    try:
        decision = await admin.post(
            f"/api/v1/admin/pilot-requests/{request_id}/decision",
            json={"decision": "APPROVED"},
        )
        assert decision.status_code == 200
        assert decision.json()["invitation_url"] is None
        assert "password" not in sender.notifications[-1].text.lower()
        settings_response = await existing.get("/api/v1/settings")
        assert settings_response.json()["is_sandbox"] is False
    finally:
        await existing.aclose()
        await admin.aclose()
