"""Integration tests against a real PostgreSQL.

Skipped when no database is reachable, so the unit suite still runs anywhere.
Postgres is real rather than faked: the append-only tables and the separation
between Action and Correction are the machinery most likely to break, and a
repository fake would pass while the real schema failed.
"""

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.shared.database import database


async def new_client(app) -> httpx.AsyncClient:
    """A fresh account, which now also means a fresh workspace."""
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    signup = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"tester-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": "Test Moderator",
        },
    )
    assert signup.status_code == 201, signup.text
    client.headers["Authorization"] = f"Bearer {signup.json()['token']}"
    return client


async def first_comment_id(client: httpx.AsyncClient, contains: str = "") -> str:
    items = (await client.get("/api/v1/comments")).json()["items"]
    matches = [i for i in items if contains in i["text"]] if contains else items
    return matches[0]["comment_id"]


@pytest.fixture
async def client():
    """An authenticated client. The moderation endpoints require a session, so
    an unauthenticated fixture would only ever prove the guard is on."""
    app = create_app()
    async with LifespanManager(app):
        if not await database.is_reachable():
            pytest.skip("no database available")
        c = await new_client(app)
        try:
            yield c
        finally:
            await c.aclose()


async def test_moderation_requires_a_session(client):
    """Client comment data must not be readable without signing in."""
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    )
    async with anonymous:
        assert (await anonymous.get("/api/v1/comments")).status_code == 401
        assert (
            await anonymous.post("/api/v1/comments/any-id/actions", json={"kind": "HIDE"})
        ).status_code == 401


async def test_actions_are_attributed_to_the_signed_in_person(client):
    """'HIDE by Test Moderator', never a hardcoded demo string."""
    comment_id = await first_comment_id(client)
    response = await client.post(
        f"/api/v1/comments/{comment_id}/actions", json={"kind": "LEAVE"}
    )
    assert response.status_code == 201
    assert response.json()[0]["actor"] == "Test Moderator"


async def test_correction_records_the_model_version_it_disagrees_with(client):
    response = await client.post(
        f"/api/v1/comments/{await first_comment_id(client)}/corrections",
        json={"severity": "SAFE", "target": "INSTITUTION", "note": "ordinary complaint"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["severity"] == "SAFE"
    assert body["target"] == "INSTITUTION"
    # Without this, a correction cannot be attributed to what it corrected.
    assert body["disagrees_with_model"] == "pattern-matching-v0.1"


async def test_hiding_a_comment_writes_no_correction(client):
    """The single most important invariant in the system.

    If actions became labels, the model would drift toward suppression while
    agreement metrics appeared to improve.
    """
    comment_id = await first_comment_id(client, "ឆ្កួត")
    before = await client.get("/api/v1/comments")
    row = next(i for i in before.json()["items"] if i["comment_id"] == comment_id)
    correction_before = row["corrected_severity"]

    hide = await client.post(f"/api/v1/comments/{comment_id}/actions", json={"kind": "HIDE"})
    assert hide.status_code == 201

    after = await client.get("/api/v1/comments")
    row_after = next(i for i in after.json()["items"] if i["comment_id"] == comment_id)

    assert row_after["latest_action"] == "HIDE"
    assert row_after["corrected_severity"] == correction_before, (
        "hiding a comment must not create or change a Correction"
    )


async def test_correction_does_not_act_on_the_comment(client):
    """Disagreeing with a label is not a decision about the comment."""
    comment_id = await first_comment_id(client, "សួស្តី")
    before = await client.get("/api/v1/comments")
    row = next(i for i in before.json()["items"] if i["comment_id"] == comment_id)
    action_before = row["latest_action"]

    await client.post(
        f"/api/v1/comments/{comment_id}/corrections",
        json={"severity": "OFFENSIVE", "target": "PERSON"},
    )

    after = await client.get("/api/v1/comments")
    row_after = next(i for i in after.json()["items"] if i["comment_id"] == comment_id)

    assert row_after["latest_action"] == action_before, (
        "submitting a Correction must not hide, unhide or leave the comment"
    )
    assert row_after["corrected_severity"] == "OFFENSIVE"


async def test_correction_on_unknown_comment_is_rejected(client):
    response = await client.post(
        "/api/v1/comments/no-such-comment/corrections",
        json={"severity": "SAFE", "target": "NEITHER"},
    )
    assert response.status_code == 404


async def test_each_account_gets_its_own_isolated_workspace():
    """Two people exploring at once must not see each other's decisions."""
    app = create_app()
    async with LifespanManager(app):
        if not await database.is_reachable():
            pytest.skip("no database available")
        alice = await new_client(app)
        bob = await new_client(app)
        try:
            alice_items = (await alice.get("/api/v1/comments")).json()["items"]
            bob_items = (await bob.get("/api/v1/comments")).json()["items"]

            assert len(alice_items) == len(bob_items) == 12
            # Same sample text, different rows.
            assert {i["comment_id"] for i in alice_items}.isdisjoint(
                {i["comment_id"] for i in bob_items}
            )

            alice_id = alice_items[0]["comment_id"]
            assert (
                await alice.post(f"/api/v1/comments/{alice_id}/actions", json={"kind": "HIDE"})
            ).status_code == 201

            # Bob must not see Alice's action...
            bob_after = (await bob.get("/api/v1/comments")).json()["items"]
            assert all(i["latest_action"] is None for i in bob_after)

            # ...nor be able to act on her comment by guessing its id.
            assert (
                await bob.post(f"/api/v1/comments/{alice_id}/actions", json={"kind": "UNHIDE"})
            ).status_code == 404
            assert (
                await bob.post(
                    f"/api/v1/comments/{alice_id}/corrections",
                    json={"severity": "SAFE", "target": "NEITHER"},
                )
            ).status_code == 404
        finally:
            await alice.aclose()
            await bob.aclose()


async def test_the_work_list_is_paginated(client):
    """A real Page produces thousands of comments; an unbounded list only ever
    worked because the data was seeded."""
    first = (await client.get("/api/v1/comments?limit=5&offset=0")).json()
    assert len(first["items"]) == 5
    assert first["total"] == 12
    assert first["limit"] == 5 and first["offset"] == 0

    second = (await client.get("/api/v1/comments?limit=5&offset=5")).json()
    assert len(second["items"]) == 5
    # Pages do not overlap.
    assert {i["comment_id"] for i in first["items"]}.isdisjoint(
        {i["comment_id"] for i in second["items"]}
    )

    last = (await client.get("/api/v1/comments?limit=5&offset=10")).json()
    assert len(last["items"]) == 2


async def test_an_absurd_page_size_is_rejected(client):
    assert (await client.get("/api/v1/comments?limit=5000")).status_code == 422
    assert (await client.get("/api/v1/comments?offset=-1")).status_code == 422


async def test_the_summary_counts_the_workspace_not_the_current_page(client):
    """The Overview previously derived its figures from whatever the work list
    returned, which becomes wrong the moment that list is paginated."""
    summary = (await client.get("/api/v1/comments/summary")).json()
    page = (await client.get("/api/v1/comments?limit=3")).json()

    assert len(page["items"]) == 3
    assert summary["processed"] == 12
    assert summary["need_review"] + 0 <= summary["processed"]
    assert sum(r["count"] for r in summary["reasons"]) == summary["need_review"]


async def test_the_summary_reflects_actions_taken(client):
    comment_id = (await client.get("/api/v1/comments")).json()["items"][0]["comment_id"]
    before = (await client.get("/api/v1/comments/summary")).json()

    await client.post(f"/api/v1/comments/{comment_id}/actions", json={"kind": "HIDE"})

    after = (await client.get("/api/v1/comments/summary")).json()
    assert after["reviewed"] == before["reviewed"] + 1
    assert after["hidden"] == before["hidden"] + 1
