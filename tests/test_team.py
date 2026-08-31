"""Workspace membership and invitations.

Invitation tokens are credentials. The rules that matter are: only owners can
issue or revoke them, a link works once, a spent or expired link cannot be
replayed, and a workspace can never be left without an owner.
"""

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.shared.database import database


async def sign_up(app, name: str = "Owner") -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    created = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"team-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": name,
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
    c = await sign_up(app, "Dara Sok")
    try:
        yield c
    finally:
        await c.aclose()


async def invite(owner_client: httpx.AsyncClient, role: str = "member") -> str:
    created = await owner_client.post("/api/v1/team/invitations", json={"role": role})
    assert created.status_code == 201, created.text
    return created.json()["token"]


# --- the team a new account starts with ------------------------------------

async def test_a_new_account_owns_a_one_person_team(owner):
    team = (await owner.get("/api/v1/team")).json()

    assert team["your_role"] == "owner"
    assert len(team["members"]) == 1
    assert team["members"][0]["display_name"] == "Dara Sok"


async def test_the_team_needs_a_session(app):
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        assert (await anonymous.get("/api/v1/team")).status_code == 401


# --- invitations ------------------------------------------------------------

async def test_an_invitation_is_returned_once_and_stored_only_as_a_hash(owner):
    created = (await owner.post("/api/v1/team/invitations", json={"role": "member"})).json()
    assert created["token"]

    listed = (await owner.get("/api/v1/team")).json()["invitations"]
    assert len(listed) == 1
    # The raw token never appears again.
    assert created["token"] != listed[0]["token_hash"]
    assert "token" not in listed[0]


async def test_a_link_shows_what_it_offers_before_anyone_signs_in(app, owner):
    token = await invite(owner)
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        preview = await anonymous.get(f"/api/v1/team/invitations/{token}/preview")
        assert preview.status_code == 200
        assert preview.json()["role"] == "member"
        # It reveals the workspace name and role, and nothing else.
        assert set(preview.json()) == {"workspace_name", "role", "expires_at"}


async def test_an_unknown_link_is_rejected(app):
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        assert (
            await anonymous.get("/api/v1/team/invitations/not-a-real-token/preview")
        ).status_code == 404


async def test_accepting_adds_the_person_to_the_team(app, owner):
    token = await invite(owner)
    joiner = await sign_up(app, "Sophea Kim")
    try:
        joined = await joiner.post(f"/api/v1/team/invitations/{token}/accept")
        assert joined.status_code == 200

        team = (await owner.get("/api/v1/team")).json()
        assert {m["display_name"] for m in team["members"]} == {"Dara Sok", "Sophea Kim"}
        # And the joiner now works in that team, not their own sandbox.
        assert (await joiner.get("/api/v1/team")).json()["workspace_id"] == team["workspace_id"]
    finally:
        await joiner.aclose()


async def test_a_link_cannot_be_replayed(app, owner):
    """The failure that matters: a link forwarded into a group chat adding
    everyone who taps it."""
    token = await invite(owner)
    first = await sign_up(app, "First")
    second = await sign_up(app, "Second")
    try:
        assert (await first.post(f"/api/v1/team/invitations/{token}/accept")).status_code == 200
        assert (await second.post(f"/api/v1/team/invitations/{token}/accept")).status_code == 409

        members = (await owner.get("/api/v1/team")).json()["members"]
        assert "Second" not in {m["display_name"] for m in members}
    finally:
        await first.aclose()
        await second.aclose()


async def test_accepting_requires_a_session(app, owner):
    token = await invite(owner)
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        assert (
            await anonymous.post(f"/api/v1/team/invitations/{token}/accept")
        ).status_code == 401


# --- only owners administer -------------------------------------------------

async def test_a_member_cannot_invite_or_remove(app, owner):
    token = await invite(owner)
    member = await sign_up(app, "Sophea Kim")
    try:
        await member.post(f"/api/v1/team/invitations/{token}/accept")

        assert (
            await member.post("/api/v1/team/invitations", json={"role": "owner"})
        ).status_code == 403

        owner_id = next(
            m["user_id"]
            for m in (await owner.get("/api/v1/team")).json()["members"]
            if m["role"] == "owner"
        )
        assert (await member.delete(f"/api/v1/team/members/{owner_id}")).status_code == 403
    finally:
        await member.aclose()


async def test_a_member_is_not_shown_invitation_tokens(app, owner):
    token = await invite(owner)
    await invite(owner)
    member = await sign_up(app, "Sophea Kim")
    try:
        await member.post(f"/api/v1/team/invitations/{token}/accept")
        assert (await member.get("/api/v1/team")).json()["invitations"] == []
        assert (await owner.get("/api/v1/team")).json()["invitations"] != []
    finally:
        await member.aclose()


async def test_an_owner_cannot_revoke_another_workspaces_invitation(app, owner):
    other_owner = await sign_up(app, "Other")
    try:
        await invite(other_owner)
        stolen = (await other_owner.get("/api/v1/team")).json()["invitations"][0]["token_hash"]
        assert (await owner.delete(f"/api/v1/team/invitations/{stolen}")).status_code == 404
        # It is still there for its actual owner.
        assert (await other_owner.get("/api/v1/team")).json()["invitations"]
    finally:
        await other_owner.aclose()


# --- a workspace always keeps an owner --------------------------------------

async def test_the_last_owner_cannot_be_removed(owner):
    """A workspace nobody can administer is unrecoverable through the product."""
    me = (await owner.get("/api/v1/team")).json()["members"][0]["user_id"]
    refused = await owner.delete(f"/api/v1/team/members/{me}")

    assert refused.status_code == 409
    assert "last owner" in refused.json()["detail"]
    assert len((await owner.get("/api/v1/team")).json()["members"]) == 1


async def test_an_owner_can_be_removed_once_another_owner_exists(app, owner):
    token = await invite(owner, role="owner")
    second = await sign_up(app, "Second Owner")
    try:
        await second.post(f"/api/v1/team/invitations/{token}/accept")

        first_id = next(
            m["user_id"]
            for m in (await owner.get("/api/v1/team")).json()["members"]
            if m["display_name"] == "Dara Sok"
        )
        assert (await second.delete(f"/api/v1/team/members/{first_id}")).status_code == 204
        assert len((await second.get("/api/v1/team")).json()["members"]) == 1
    finally:
        await second.aclose()


async def test_removing_someone_who_is_not_a_member_is_a_404(owner):
    assert (await owner.delete("/api/v1/team/members/nobody")).status_code == 404
