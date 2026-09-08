"""Workspace-scoped Automated Replies API contract."""

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.shared.database import database


async def sign_up(app, name: str = "Reply Owner") -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    created = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"reply-{uuid.uuid4().hex[:12]}@example.com",
            "password": "a-long-enough-password",
            "display_name": name,
            "organization": "Reply Lab",
        },
    )
    assert created.status_code == 201, created.text
    client.headers["Authorization"] = f"Bearer {created.json()['token']}"
    return client


@pytest.fixture
async def app():
    application = create_app()
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        yield application


@pytest.fixture
async def owner(app):
    client = await sign_up(app)
    try:
        yield client
    finally:
        await client.aclose()


async def test_automated_replies_start_disabled(owner):
    response = await owner.get("/api/v1/auto-replies/settings")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": False,
        "your_role": "owner",
    }


async def test_owner_can_create_and_simulate_a_safe_rule(owner):
    created = await owner.post(
        "/api/v1/auto-replies/rules",
        json={
            "name": "Price question",
            "keywords": ["price"],
            "reply_body": "Please send us a message for today’s price.",
            "on_comments": True,
            "on_messages": False,
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["position"] == 0
    assert created.json()["warnings"] == []

    disabled = await owner.post(
        "/api/v1/auto-replies/simulate", json={"text": "What is the price?"}
    )
    assert disabled.json()["kind"] == "OFF"

    enabled = await owner.patch(
        "/api/v1/auto-replies/settings", json={"enabled": True}
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json() == {"enabled": True, "your_role": "owner"}

    simulated = await owner.post(
        "/api/v1/auto-replies/simulate", json={"text": "What is the price?"}
    )
    assert simulated.json() == {
        "kind": "REPLY",
        "rule_id": created.json()["id"],
        "reply_body": "Please send us a message for today’s price.",
        "reason": None,
    }

    unsafe = await owner.post(
        "/api/v1/auto-replies/simulate", json={"text": "price ឆ្កួត"}
    )
    assert unsafe.json()["kind"] == "UNSAFE"
    assert unsafe.json()["reply_body"] is None


async def test_invalid_short_keyword_is_refused(owner):
    response = await owner.post(
        "/api/v1/auto-replies/rules",
        json={
            "name": "Too short",
            "keywords": ["ab"],
            "reply_body": "Thanks.",
        },
    )

    assert response.status_code == 422
    assert "at least 3 characters" in response.json()["detail"]


async def test_member_cannot_manage_automated_replies(app, owner):
    invitation = await owner.post(
        "/api/v1/team/invitations", json={"role": "member"}
    )
    assert invitation.status_code == 201, invitation.text
    member = await sign_up(app, "Reply Member")
    try:
        accepted = await member.post(
            f"/api/v1/team/invitations/{invitation.json()['token']}/accept"
        )
        assert accepted.status_code == 200, accepted.text
        assert (await member.patch(
            "/api/v1/auto-replies/settings", json={"enabled": True}
        )).status_code == 403
        assert (await member.post(
            "/api/v1/auto-replies/rules",
            json={"name": "Nope", "keywords": ["price"], "reply_body": "Nope."},
        )).status_code == 403
    finally:
        await member.aclose()


async def test_auto_reply_contract_requires_authentication(app):
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    async with anonymous:
        assert (await anonymous.get("/api/v1/auto-replies/rules")).status_code == 401
