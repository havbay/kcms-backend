"""Integration tests against a real PostgreSQL.

Skipped when no database is reachable, so the unit suite still runs anywhere.
Postgres is real rather than faked: the append-only tables and the separation
between Action and Correction are the machinery most likely to break, and a
repository fake would pass while the real schema failed.
"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.shared.database import database


@pytest.fixture
async def client():
    app = create_app()
    async with LifespanManager(app):
        if not await database.is_reachable():
            pytest.skip("no database available")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_correction_records_the_model_version_it_disagrees_with(client):
    response = await client.post(
        "/api/v1/comments/c-001/corrections",
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
    before = await client.get("/api/v1/comments")
    row = next(i for i in before.json()["items"] if i["comment_id"] == "c-005")
    correction_before = row["corrected_severity"]

    hide = await client.post("/api/v1/comments/c-005/actions", json={"kind": "HIDE"})
    assert hide.status_code == 201

    after = await client.get("/api/v1/comments")
    row_after = next(i for i in after.json()["items"] if i["comment_id"] == "c-005")

    assert row_after["latest_action"] == "HIDE"
    assert row_after["corrected_severity"] == correction_before, (
        "hiding a comment must not create or change a Correction"
    )


async def test_correction_does_not_act_on_the_comment(client):
    """Disagreeing with a label is not a decision about the comment."""
    before = await client.get("/api/v1/comments")
    row = next(i for i in before.json()["items"] if i["comment_id"] == "c-009")
    action_before = row["latest_action"]

    await client.post(
        "/api/v1/comments/c-009/corrections",
        json={"severity": "OFFENSIVE", "target": "PERSON"},
    )

    after = await client.get("/api/v1/comments")
    row_after = next(i for i in after.json()["items"] if i["comment_id"] == "c-009")

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
