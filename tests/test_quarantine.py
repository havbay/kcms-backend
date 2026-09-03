"""Quarantine auto-delete: a HARMFUL comment is hidden immediately and only
actually deleted once the workspace's configured delay expires with no human
intervention. delay=0 keeps the pre-quarantine behaviour unchanged — straight
to DELETE, no HIDE step.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from cryptography.fernet import Fernet

from kcms.app import create_app
from kcms.integrations.contracts import ProviderComment, ProviderPage
from kcms.integrations.facebook import get_meta_client, get_optional_meta_client
from kcms.moderation import quarantine
from kcms.shared.database import database

PAGE_ID = "quarantine-page-1"
# Matches data/keywords.json's HARMFUL list ("ឆ្កួត", "ងាប់") — the same text
# test_comment_sync.py already relies on for HARMFUL classification.
HARMFUL_TEXT = "អ្នកនេះឆ្កួតណាស់ ងាប់ទៅ"
# "ឆោត" (foolish) is OFFENSIVE-only in the shipped list, with no HARMFUL hit —
# a clean OFFENSIVE-severity, PERSON-target comment.
OFFENSIVE_TEXT = "អ្នកនេះឆោតណាស់"


class RecordingMetaClient:
    """The FastAPI-layer Meta client — what `/sync` and `/actions` talk to."""

    def __init__(self):
        self.deleted: list[str] = []
        self.hidden: list[tuple[str, bool]] = []
        self.comments: list[ProviderComment] = []

    async def validate_page_token(self, token: str) -> ProviderPage:
        return ProviderPage(
            page_id=PAGE_ID,
            page_name="Demo Page",
            access_token=token,
            tasks=("PROFILE_PLUS_MODERATE",),
        )

    def authorization_url(self, state: str) -> str:
        return f"https://facebook.example/authorize?state={state}"

    async def exchange_code(self, code: str) -> str:
        return "user-token"

    async def list_pages(self, user_token: str) -> list[ProviderPage]:
        return []

    async def fetch_comments(self, page_id: str, token: str) -> list[ProviderComment]:
        return list(self.comments)

    async def set_comment_hidden(self, comment_id: str, token: str, hidden: bool) -> None:
        self.hidden.append((comment_id, hidden))

    async def delete_comment(self, comment_id: str, token: str) -> None:
        self.deleted.append(comment_id)


class FakeGraphMetaClient:
    """What `quarantine.sweep_once` talks to. It builds its own Graph client
    from settings rather than through FastAPI DI, so this patches the class
    itself (`kcms.moderation.quarantine.GraphMetaClient`) instead."""

    deleted: list[str] = []
    refuse = False

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def delete_comment(self, comment_id: str, token: str) -> None:
        if FakeGraphMetaClient.refuse:
            raise ValueError("Meta rejected the request: permission missing")
        FakeGraphMetaClient.deleted.append(comment_id)


def provider_comment(comment_id: str, text: str) -> ProviderComment:
    return ProviderComment(
        comment_id=comment_id,
        text=text,
        created_time=datetime.now(UTC),
        author_ref=f"fb:{comment_id}",
    )


@pytest.fixture
def meta() -> RecordingMetaClient:
    return RecordingMetaClient()


@pytest.fixture
async def app(meta, monkeypatch):
    from kcms.settings import settings as app_settings

    # sweep_once needs Meta + encryption "configured" and a Graph client that
    # never touches the network. The credential DI stays real (not
    # overridden) so a Page connected through the HTTP layer is encrypted
    # with the exact same key sweep_once will later decrypt with.
    monkeypatch.setattr(app_settings, "meta_graph_version", "v21.0")
    monkeypatch.setattr(app_settings, "integration_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("kcms.moderation.quarantine.GraphMetaClient", FakeGraphMetaClient)
    FakeGraphMetaClient.deleted = []
    FakeGraphMetaClient.refuse = False

    application = create_app()
    application.dependency_overrides[get_meta_client] = lambda: meta
    application.dependency_overrides[get_optional_meta_client] = lambda: meta
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        async with database.acquire() as connection:
            await connection.execute(
                "DELETE FROM page_connection WHERE external_page_id = $1", PAGE_ID
            )
            for table in ("action", "verdict", "correction"):
                await connection.execute(
                    f"DELETE FROM {table} WHERE comment_id IN "
                    "(SELECT comment_id FROM comment_content WHERE page_id = $1)",
                    PAGE_ID,
                )
            await connection.execute("DELETE FROM comment_content WHERE page_id = $1", PAGE_ID)
        yield application


async def connected_client(app) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    created = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"q-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": "Dara Sok",
            "organization": "Quarantine Shop",
        },
    )
    assert created.status_code == 201, created.text
    client.headers["Authorization"] = f"Bearer {created.json()['token']}"
    connected = await client.post(
        "/api/v1/facebook/connections/manual",
        json={"page_access_token": "a-valid-page-token-for-test"},
    )
    assert connected.status_code == 201, connected.text
    return client


async def test_delay_zero_deletes_instantly_unchanged_from_before(app, meta):
    meta.comments = [provider_comment("q-c-1", HARMFUL_TEXT)]
    client = await connected_client(app)
    try:
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.deleted == ["q-c-1"]
        assert meta.hidden == []
        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        row = next(i for i in listed["items"] if i["comment_id"] == "q-c-1")
        assert row["latest_action"] == "DELETE"
        assert row["pending_delete_at"] is None
    finally:
        await client.aclose()


async def test_a_positive_delay_hides_now_and_schedules_the_delete(app, meta):
    client = await connected_client(app)
    try:
        set_delay = await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 30})
        assert set_delay.status_code == 200, set_delay.text

        meta.comments = [provider_comment("q-c-2", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.hidden == [("q-c-2", True)]
        assert meta.deleted == []

        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        row = next(i for i in listed["items"] if i["comment_id"] == "q-c-2")
        assert row["latest_action"] == "HIDE"
        assert row["pending_delete_at"] is not None

        async with database.acquire() as connection:
            scheduled = await connection.fetchrow(
                "SELECT page_id, scheduled_for FROM scheduled_deletion WHERE comment_id = $1",
                "q-c-2",
            )
        assert scheduled is not None
        assert scheduled["page_id"] == PAGE_ID
        # ~30 minutes out — not instant, and not some other delay.
        remaining = scheduled["scheduled_for"] - datetime.now(UTC)
        assert timedelta(minutes=28) < remaining < timedelta(minutes=31)
    finally:
        await client.aclose()


async def test_leaving_a_quarantined_comment_cancels_the_scheduled_delete(app, meta):
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 60})
        meta.comments = [provider_comment("q-c-3", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        left = await client.post("/api/v1/comments/q-c-3/actions", json={"kind": "LEAVE"})
        assert left.status_code == 201, left.text

        async with database.acquire() as connection:
            scheduled = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", "q-c-3"
            )
        assert scheduled == 0

        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        row = next(i for i in listed["items"] if i["comment_id"] == "q-c-3")
        assert row["pending_delete_at"] is None

        # Nothing left for the sweep to find for this comment.
        deleted = await quarantine.sweep_once()
        assert deleted == 0
        assert FakeGraphMetaClient.deleted == []
    finally:
        await client.aclose()


async def test_unhiding_a_quarantined_comment_also_cancels_the_delete(app, meta):
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 60})
        meta.comments = [provider_comment("q-c-4", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        unhidden = await client.post("/api/v1/comments/q-c-4/actions", json={"kind": "UNHIDE"})
        assert unhidden.status_code == 201, unhidden.text
        assert meta.hidden == [("q-c-4", True), ("q-c-4", False)]

        async with database.acquire() as connection:
            scheduled = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", "q-c-4"
            )
        assert scheduled == 0
    finally:
        await client.aclose()


async def test_a_manual_delete_also_cancels_any_pending_schedule(app, meta):
    """A no-op in practice — the comment can't be deleted twice — but this
    confirms cancellation happens unconditionally rather than only for
    LEAVE/UNHIDE."""
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 60})
        meta.comments = [provider_comment("q-c-5", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        deleted = await client.post("/api/v1/comments/q-c-5/actions", json={"kind": "DELETE"})
        assert deleted.status_code == 201, deleted.text

        async with database.acquire() as connection:
            scheduled = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", "q-c-5"
            )
        assert scheduled == 0
    finally:
        await client.aclose()


async def test_sweep_deletes_a_comment_whose_delay_has_expired(app, meta):
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 1440})
        meta.comments = [provider_comment("q-c-6", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        # Force it due now instead of waiting a day.
        async with database.acquire() as connection:
            await connection.execute(
                "UPDATE scheduled_deletion SET scheduled_for = NOW() - INTERVAL '1 minute' "
                "WHERE comment_id = $1",
                "q-c-6",
            )

        deleted = await quarantine.sweep_once()

        assert deleted == 1
        assert FakeGraphMetaClient.deleted == ["q-c-6"]
        async with database.acquire() as connection:
            remaining = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", "q-c-6"
            )
            action = await connection.fetchrow(
                "SELECT kind, provider_applied FROM action WHERE comment_id = $1 "
                "ORDER BY occurred_at DESC, id DESC LIMIT 1",
                "q-c-6",
            )
        assert remaining == 0
        assert action["kind"] == "DELETE"
        assert action["provider_applied"] is True
    finally:
        await client.aclose()


async def test_sweep_leaves_the_schedule_for_retry_when_facebook_refuses(app, meta):
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 1440})
        meta.comments = [provider_comment("q-c-7", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        async with database.acquire() as connection:
            await connection.execute(
                "UPDATE scheduled_deletion SET scheduled_for = NOW() - INTERVAL '1 minute' "
                "WHERE comment_id = $1",
                "q-c-7",
            )

        FakeGraphMetaClient.refuse = True
        deleted = await quarantine.sweep_once()

        assert deleted == 0
        async with database.acquire() as connection:
            remaining = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", "q-c-7"
            )
        # Still there — the next tick will try again.
        assert remaining == 1
    finally:
        await client.aclose()


async def test_seeded_sample_comments_are_never_quarantined(app, meta):
    """Samples never reach a real Page, so hiding/scheduling them would mean
    nothing. They keep the instant, DB-only DELETE regardless of the
    workspace's configured delay."""
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-delete", json={"delay_minutes": 1440})
        workspace_id = (await client.get("/api/v1/settings")).json()["workspace_id"]
        async with database.acquire() as connection:
            harmful_sample = await connection.fetchval(
                """SELECT c.comment_id FROM comment_content c
                   JOIN verdict v ON v.comment_id = c.comment_id
                   WHERE c.workspace_id = $1 AND c.page_id = 'demo-page' AND v.severity = 'HARMFUL'
                   ORDER BY v.occurred_at DESC LIMIT 1""",
                workspace_id,
            )
        assert harmful_sample, "expected a seeded HARMFUL sample to exist"

        async with database.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT kind FROM action WHERE comment_id = $1", harmful_sample
            )
            scheduled = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", harmful_sample
            )
        assert row is not None and row["kind"] == "DELETE"
        assert scheduled == 0
    finally:
        await client.aclose()


async def test_offensive_comments_are_left_alone_by_default(app, meta):
    """Baseline: with the toggle off, OFFENSIVE surfaces for a human but
    nothing is hidden."""
    meta.comments = [provider_comment("q-c-8", OFFENSIVE_TEXT)]
    client = await connected_client(app)
    try:
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.hidden == []
        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        row = next(i for i in listed["items"] if i["comment_id"] == "q-c-8")
        assert row["severity"] == "OFFENSIVE"
        assert row["latest_action"] is None
    finally:
        await client.aclose()


async def test_auto_hide_offensive_hides_immediately_with_no_schedule(app, meta):
    client = await connected_client(app)
    try:
        toggled = await client.patch(
            "/api/v1/settings/auto-hide-offensive", json={"enabled": True}
        )
        assert toggled.status_code == 200, toggled.text
        assert toggled.json()["auto_hide_offensive"] is True

        meta.comments = [provider_comment("q-c-9", OFFENSIVE_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.hidden == [("q-c-9", True)]
        assert meta.deleted == []

        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        row = next(i for i in listed["items"] if i["comment_id"] == "q-c-9")
        assert row["severity"] == "OFFENSIVE"
        assert row["latest_action"] == "HIDE"
        # Never scheduled: OFFENSIVE always waits on a person to delete or leave.
        assert row["pending_delete_at"] is None

        async with database.acquire() as connection:
            scheduled = await connection.fetchval(
                "SELECT COUNT(*) FROM scheduled_deletion WHERE comment_id = $1", "q-c-9"
            )
            action = await connection.fetchrow(
                "SELECT kind, actor FROM action WHERE comment_id = $1", "q-c-9"
            )
        assert scheduled == 0
        assert action["kind"] == "HIDE"
        assert action["actor"] == "system:auto-hide-offensive"
    finally:
        await client.aclose()


async def test_auto_hide_offensive_never_touches_harmful_comments(app, meta):
    """HARMFUL still goes through the quarantine/instant-delete path, not
    this one — the two toggles are independent."""
    client = await connected_client(app)
    try:
        await client.patch("/api/v1/settings/auto-hide-offensive", json={"enabled": True})
        meta.comments = [provider_comment("q-c-10", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.deleted == ["q-c-10"]
        assert meta.hidden == []
    finally:
        await client.aclose()


async def test_a_member_cannot_change_auto_hide_offensive(app, meta):
    client = await connected_client(app)
    try:
        invitation = await client.post("/api/v1/team/invitations", json={"role": "member"})
        token = invitation.json()["token"]
        member = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            created = await member.post(
                "/api/v1/auth/signup",
                json={
                    "email": f"q-member-{uuid.uuid4().hex[:10]}@example.com",
                    "password": "a-long-enough-password",
                    "display_name": "Sophea Kim",
                    "organization": "Joining",
                },
            )
            member.headers["Authorization"] = f"Bearer {created.json()['token']}"
            await member.post(f"/api/v1/team/invitations/{token}/accept")

            refused = await member.patch(
                "/api/v1/settings/auto-hide-offensive", json={"enabled": True}
            )
            assert refused.status_code == 403
        finally:
            await member.aclose()
    finally:
        await client.aclose()


async def test_the_blocklist_forces_a_comment_harmful_end_to_end(app, meta):
    client = await connected_client(app)
    try:
        blocked = await client.patch(
            "/api/v1/settings/keyword-blocklist", json={"keywords": ["ឆ្ងាញ់"]}
        )
        assert blocked.status_code == 200, blocked.text
        assert blocked.json()["keyword_blocklist"] == ["ឆ្ងាញ់"]

        meta.comments = [provider_comment("q-c-11", "អាហារនេះឆ្ងាញ់ណាស់")]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.deleted == ["q-c-11"]  # HARMFUL + delay=0 default
        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        row = next(i for i in listed["items"] if i["comment_id"] == "q-c-11")
        assert row["severity"] == "HARMFUL"
        assert "ឆ្ងាញ់" in (row["rationale"] or "")
    finally:
        await client.aclose()


async def test_the_allowlist_clears_a_comment_end_to_end(app, meta):
    """SAFE-and-cleared comments never surface in the queue at all, so this
    checks the record directly rather than through /comments."""
    client = await connected_client(app)
    try:
        allowed = await client.patch(
            "/api/v1/settings/keyword-allowlist", json={"keywords": ["ឆ្កួត"]}
        )
        assert allowed.status_code == 200, allowed.text

        meta.comments = [provider_comment("q-c-12", HARMFUL_TEXT)]
        await client.post(f"/api/v1/facebook/connections/{PAGE_ID}/sync")

        assert meta.deleted == []
        assert meta.hidden == []
        async with database.acquire() as connection:
            verdict = await connection.fetchrow(
                "SELECT severity, surfaced_reason, rationale FROM verdict "
                "WHERE comment_id = $1 ORDER BY occurred_at DESC, id DESC LIMIT 1",
                "q-c-12",
            )
        assert verdict["severity"] == "SAFE"
        assert verdict["surfaced_reason"] == "cleared"
        assert "ឆ្កួត" in (verdict["rationale"] or "")
    finally:
        await client.aclose()


async def test_keyword_list_rejects_a_phrase_that_is_too_long(app, meta):
    client = await connected_client(app)
    try:
        refused = await client.patch(
            "/api/v1/settings/keyword-blocklist", json={"keywords": ["x" * 101]}
        )
        assert refused.status_code == 422
    finally:
        await client.aclose()
