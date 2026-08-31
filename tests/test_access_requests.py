"""Page connection requests, and the administration boundary around them.

The rule that Platform Administrators cannot read customer comments has been in
the specification since the beginning. This is the first surface where it is
testable rather than aspirational, so it is tested by shape, not by inspection.
"""

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.settings import settings
from kcms.shared.database import database

ADMIN_EMAIL = "boundary-admin@example.com"

VALID_REQUEST = {
    "page_name": "facebook.com/angkorshop",
    "monthly_comments": "1K_TO_10K",
    "team_size": "2_TO_5",
    "note": "We get a lot of scam replies on product posts.",
}


async def sign_up(app, email: str | None = None) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email or f"client-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": "Dara Sok",
            "organization": "Angkor Shop",
        },
    )
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"
    return client


@pytest.fixture
async def app():
    application = create_app()
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        yield application


@pytest.fixture
async def client(app):
    c = await sign_up(app)
    try:
        yield c
    finally:
        await c.aclose()


@pytest.fixture
async def admin(app, monkeypatch):
    """An administrator, granted only through the environment allowlist."""
    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(settings, "platform_admin_emails", email)
    c = await sign_up(app, email)
    try:
        yield c
    finally:
        await c.aclose()


# --- the client side -------------------------------------------------------

async def test_a_sandbox_workspace_can_request_a_page_connection(client):
    response = await client.post("/api/v1/access-requests", json=VALID_REQUEST)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["page_name"] == "facebook.com/angkorshop"
    assert body["decision_reason"] is None


async def test_resubmitting_replaces_the_open_request_rather_than_queueing(client):
    """One impatient client must not fill the administrator's queue."""
    first = (await client.post("/api/v1/access-requests", json=VALID_REQUEST)).json()
    second = (
        await client.post(
            "/api/v1/access-requests", json={**VALID_REQUEST, "page_name": "facebook.com/other"}
        )
    ).json()

    assert first["id"] != second["id"]
    mine = (await client.get("/api/v1/access-requests/mine")).json()
    assert mine["id"] == second["id"]
    assert mine["page_name"] == "facebook.com/other"


async def test_mine_is_empty_before_anything_is_submitted(client):
    assert (await client.get("/api/v1/access-requests/mine")).json() is None


async def test_a_request_needs_a_session(app):
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    async with anonymous:
        assert (
            await anonymous.post("/api/v1/access-requests", json=VALID_REQUEST)
        ).status_code == 401


# --- the administration boundary -------------------------------------------

async def test_an_ordinary_client_cannot_reach_administration(client):
    """The failure that matters: any signed-in account listing every workspace."""
    assert (await client.get("/api/v1/admin/access-requests")).status_code == 403
    assert (
        await client.post(
            "/api/v1/admin/access-requests/anything/decision", json={"decision": "APPROVED"}
        )
    ).status_code == 403


async def test_administration_is_not_reachable_without_a_session(app):
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    async with anonymous:
        assert (await anonymous.get("/api/v1/admin/access-requests")).status_code == 401


async def test_the_admin_list_never_carries_comment_content(client, admin):
    """Asserted on the response shape, not by reading it.

    A later 'just a small preview' field would fail here rather than quietly
    handing customer comments to platform staff.
    """
    await client.post("/api/v1/access-requests", json=VALID_REQUEST)
    rows = (await admin.get("/api/v1/admin/access-requests")).json()
    assert rows

    forbidden = {"text", "comment", "comments", "comment_id", "post_text", "parent_text"}

    def walk(node, path="root"):
        if isinstance(node, dict):
            leaked = forbidden.intersection(node)
            assert not leaked, f"comment content leaked at {path}: {leaked}"
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(rows)
    assert {"workspace_name", "requester_name", "page_name"} <= set(rows[0])


# --- decisions -------------------------------------------------------------

async def test_approving_lifts_the_sandbox_restriction(client, admin):
    created = (await client.post("/api/v1/access-requests", json=VALID_REQUEST)).json()

    decided = await admin.post(
        f"/api/v1/admin/access-requests/{created['id']}/decision",
        json={"decision": "APPROVED"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "APPROVED"

    # The workspace is no longer a sandbox, so it cannot request again.
    again = await client.post("/api/v1/access-requests", json=VALID_REQUEST)
    assert again.status_code == 409


async def test_declining_requires_a_reason_the_client_can_act_on(client, admin):
    created = (await client.post("/api/v1/access-requests", json=VALID_REQUEST)).json()

    without = await admin.post(
        f"/api/v1/admin/access-requests/{created['id']}/decision",
        json={"decision": "DECLINED"},
    )
    assert without.status_code == 422

    with_reason = await admin.post(
        f"/api/v1/admin/access-requests/{created['id']}/decision",
        json={"decision": "DECLINED", "reason": "Page is not currently reachable."},
    )
    assert with_reason.status_code == 200

    mine = (await client.get("/api/v1/access-requests/mine")).json()
    assert mine["status"] == "DECLINED"
    assert mine["decision_reason"] == "Page is not currently reachable."


async def test_a_decided_request_cannot_be_decided_again(client, admin):
    created = (await client.post("/api/v1/access-requests", json=VALID_REQUEST)).json()
    url = f"/api/v1/admin/access-requests/{created['id']}/decision"

    assert (await admin.post(url, json={"decision": "APPROVED"})).status_code == 200
    assert (await admin.post(url, json={"decision": "DECLINED", "reason": "x"})).status_code == 409


async def test_the_admin_flag_is_reported_but_never_accepted_from_the_client(app, monkeypatch):
    """The frontend needs to know whether to show administration navigation.
    Reporting it must not create a way to claim it."""
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(settings, "platform_admin_emails", email)
    admin = await sign_up(app, email)
    try:
        assert (await admin.get("/api/v1/auth/me")).json()["is_platform_admin"] is True
    finally:
        await admin.aclose()

    # An ordinary account cannot grant itself the role by asking for it.
    ordinary = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    async with ordinary:
        created = await ordinary.post(
            "/api/v1/auth/signup",
            json={
                "email": f"sneaky-{uuid.uuid4().hex[:8]}@example.com",
                "password": "a-long-enough-password",
                "display_name": "Sneaky",
                "is_platform_admin": True,
            },
        )
        assert created.status_code == 201
        ordinary.headers["Authorization"] = f"Bearer {created.json()['token']}"
        assert (await ordinary.get("/api/v1/auth/me")).json()["is_platform_admin"] is False
        assert (await ordinary.get("/api/v1/admin/access-requests")).status_code == 403


async def test_removing_an_email_from_the_allowlist_revokes_the_role(app, monkeypatch):
    """Reconciled at sign-in, so revocation actually takes effect."""
    email = f"temp-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(settings, "platform_admin_emails", email)
    granted = await sign_up(app, email)
    try:
        assert (await granted.get("/api/v1/auth/me")).json()["is_platform_admin"] is True
    finally:
        await granted.aclose()

    monkeypatch.setattr(settings, "platform_admin_emails", "")
    revoked = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    async with revoked:
        session = await revoked.post(
            "/api/v1/auth/signin", json={"email": email, "password": "a-long-enough-password"}
        )
        assert session.status_code == 200
        revoked.headers["Authorization"] = f"Bearer {session.json()['token']}"
        assert (await revoked.get("/api/v1/auth/me")).json()["is_platform_admin"] is False
        assert (await revoked.get("/api/v1/admin/access-requests")).status_code == 403
