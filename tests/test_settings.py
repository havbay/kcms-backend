"""Workspace and personal settings.

Renaming a workspace affects everyone in it; renaming yourself affects only
future attribution. The two carry different authorization for that reason.
"""

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.shared.database import database


async def sign_up(app, name: str = "Dara Sok") -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    created = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"set-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": name,
            "organization": "Angkor Shop",
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
    c = await sign_up(app)
    try:
        yield c
    finally:
        await c.aclose()


async def test_settings_describe_the_workspace_and_the_person(owner):
    body = (await owner.get("/api/v1/settings")).json()

    assert body["workspace_name"] == "Angkor Shop"
    assert body["display_name"] == "Dara Sok"
    assert body["your_role"] == "owner"
    assert body["is_sandbox"] is True


async def test_settings_need_a_session(app):
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        assert (await anonymous.get("/api/v1/settings")).status_code == 401


async def test_an_owner_can_rename_the_workspace(owner):
    renamed = await owner.patch("/api/v1/settings/workspace", json={"name": "Angkor Media"})

    assert renamed.status_code == 200
    assert renamed.json()["workspace_name"] == "Angkor Media"
    assert (await owner.get("/api/v1/settings")).json()["workspace_name"] == "Angkor Media"


async def test_a_member_cannot_rename_the_shared_workspace(app, owner):
    """A rename is visible to everyone in the workspace, so it is an owner action."""
    token = (await owner.post("/api/v1/team/invitations", json={"role": "member"})).json()["token"]
    member = await sign_up(app, "Sophea Kim")
    try:
        await member.post(f"/api/v1/team/invitations/{token}/accept")
        refused = await member.patch("/api/v1/settings/workspace", json={"name": "Hijacked"})
        assert refused.status_code == 403
        assert (await owner.get("/api/v1/settings")).json()["workspace_name"] == "Angkor Shop"
    finally:
        await member.aclose()


async def test_anyone_can_rename_themselves(app, owner):
    token = (await owner.post("/api/v1/team/invitations", json={"role": "member"})).json()["token"]
    member = await sign_up(app, "Sophea Kim")
    try:
        await member.post(f"/api/v1/team/invitations/{token}/accept")
        renamed = await member.patch("/api/v1/settings/me", json={"display_name": "Sophea K."})
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "Sophea K."

        names = {m["display_name"] for m in (await owner.get("/api/v1/team")).json()["members"]}
        assert "Sophea K." in names
    finally:
        await member.aclose()


async def test_a_rename_does_not_rewrite_who_took_past_actions(owner):
    """The audit trail records who acted under the name used at the time.
    Rewriting it would let someone quietly disown a decision."""
    comment_id = (await owner.get("/api/v1/comments")).json()["items"][0]["comment_id"]
    await owner.post(f"/api/v1/comments/{comment_id}/actions", json={"kind": "HIDE"})

    await owner.patch("/api/v1/settings/me", json={"display_name": "Someone Else"})

    row = next(
        i for i in (await owner.get("/api/v1/comments")).json()["items"]
        if i["comment_id"] == comment_id
    )
    assert row["latest_actor"] == "Dara Sok"


async def test_a_blank_name_is_rejected(owner):
    assert (await owner.patch("/api/v1/settings/workspace", json={"name": ""})).status_code == 422
    assert (await owner.patch("/api/v1/settings/me", json={"display_name": ""})).status_code == 422
