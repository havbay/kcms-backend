"""Comments reach KCMS from a connected Page, and hiding reaches Facebook.

These cover the demo path end to end: connect a Page, sync its comments into
the work list, hide one, and confirm the hide was applied on Facebook rather
than only recorded locally.
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.integrations.contracts import ProviderComment, ProviderPage
from kcms.integrations.credentials import get_credential_cipher, get_optional_credential_cipher
from kcms.integrations.facebook import get_meta_client, get_optional_meta_client
from kcms.shared.database import database

PAGE_ID = "sync-page-1"


class TestCipher:
    def seal(self, value: str) -> str:
        return f"sealed::{value[::-1]}"

    def open(self, value: str) -> str:
        assert value.startswith("sealed::")
        return value.removeprefix("sealed::")[::-1]


class RecordingMetaClient:
    """Records what reached Facebook so a test can prove it happened."""

    def __init__(self):
        self.hidden: list[tuple[str, bool]] = []
        self.comments: list[ProviderComment] = []
        self.refuse = False

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
        assert page_id == PAGE_ID
        return list(self.comments)

    async def set_comment_hidden(self, comment_id: str, token: str, hidden: bool) -> None:
        if self.refuse:
            raise ValueError("Meta rejected the request: permission missing")
        self.hidden.append((comment_id, hidden))


def provider_comment(comment_id: str, text: str) -> ProviderComment:
    return ProviderComment(
        comment_id=comment_id,
        text=text,
        created_time=datetime.now(UTC),
        author_ref=f"fb:{comment_id}",
        post_text="តើសេវាកម្មយើងយ៉ាងណា?",
        post_permalink="https://facebook.com/permalink",
        post_kind="TEXT",
    )


@pytest.fixture
def meta() -> RecordingMetaClient:
    return RecordingMetaClient()


@pytest.fixture
async def app(meta):
    application = create_app()
    application.dependency_overrides[get_meta_client] = lambda: meta
    application.dependency_overrides[get_optional_meta_client] = lambda: meta
    application.dependency_overrides[get_credential_cipher] = lambda: TestCipher()
    application.dependency_overrides[get_optional_credential_cipher] = lambda: TestCipher()
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        async with database.acquire() as connection:
            await connection.execute(
                "DELETE FROM page_connection WHERE external_page_id = $1", PAGE_ID
            )
            # Verdicts, actions and corrections are append-only, so deleting the
            # content alone would leave a previous test's action attached to the
            # same provider comment id.
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
            "email": f"sync-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": "Dara Sok",
            "organization": "Demo Shop",
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


async def test_sync_imports_page_comments_into_the_work_list(app, meta):
    meta.comments = [
        provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់ ខ្ញុំមិនពេញចិត្តទេ"),
        provider_comment("fb-c-2", "អរគុណច្រើន សេវាកម្មល្អ"),
    ]
    client = await connected_client(app)
    try:
        result = await client.post("/api/v1/facebook/sync")
        assert result.status_code == 200, result.text
        assert result.json()["fetched"] == 2
        assert result.json()["imported"] == 2
        assert result.json()["last_synced_at"] is not None

        listed = await client.get("/api/v1/comments", params={"query": "សេវាកម្មនេះយឺត"})
        items = listed.json()["items"]
        assert [item["comment_id"] for item in items] == ["fb-c-1"]
        # A verdict is produced on arrival, so the comment is triaged, not raw.
        assert items[0]["severity"] is not None
        assert items[0]["page_id"] == PAGE_ID
    finally:
        await client.aclose()


async def test_resyncing_the_same_page_does_not_duplicate_or_reset_a_comment(app, meta):
    meta.comments = [provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់")]
    client = await connected_client(app)
    try:
        first = await client.post("/api/v1/facebook/sync")
        assert first.json()["imported"] == 1

        acted = await client.post(
            "/api/v1/comments/fb-c-1/actions", json={"kind": "HIDE"}
        )
        assert acted.status_code == 201, acted.text

        second = await client.post("/api/v1/facebook/sync")
        assert second.status_code == 200
        # Fetched again, imported zero: the existing row is left alone.
        assert second.json()["fetched"] == 1
        assert second.json()["imported"] == 0

        listed = await client.get("/api/v1/comments", params={"query": "សេវាកម្មនេះយឺត"})
        assert listed.json()["total"] == 1
        # The action survived the re-sync rather than being wiped by re-import.
        assert listed.json()["items"][0]["latest_action"] == "HIDE"
    finally:
        await client.aclose()


async def test_hiding_a_page_comment_is_applied_on_facebook(app, meta):
    meta.comments = [provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់")]
    client = await connected_client(app)
    try:
        await client.post("/api/v1/facebook/sync")

        hidden = await client.post("/api/v1/comments/fb-c-1/actions", json={"kind": "HIDE"})
        assert hidden.status_code == 201, hidden.text
        assert meta.hidden == [("fb-c-1", True)]

        shown = await client.post("/api/v1/comments/fb-c-1/actions", json={"kind": "UNHIDE"})
        assert shown.status_code == 201, shown.text
        assert meta.hidden == [("fb-c-1", True), ("fb-c-1", False)]
    finally:
        await client.aclose()


async def test_leaving_a_comment_visible_never_calls_facebook(app, meta):
    meta.comments = [provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់")]
    client = await connected_client(app)
    try:
        await client.post("/api/v1/facebook/sync")
        left = await client.post("/api/v1/comments/fb-c-1/actions", json={"kind": "LEAVE"})
        assert left.status_code == 201
        assert meta.hidden == []
    finally:
        await client.aclose()


async def test_hiding_a_seeded_sample_comment_never_calls_facebook(app, meta):
    """Sample comments are not on the connected Page, so hiding one must not
    send a hide for an id Facebook does not know."""
    client = await connected_client(app)
    try:
        workspace_id = (await client.get("/api/v1/settings")).json()["workspace_id"]
        async with database.acquire() as connection:
            seeded_id = await connection.fetchval(
                "SELECT comment_id FROM comment_content "
                "WHERE workspace_id = $1 AND page_id = 'demo-page' LIMIT 1",
                workspace_id,
            )
        assert seeded_id, "expected the workspace to have seeded samples"
        acted = await client.post(
            f"/api/v1/comments/{seeded_id}/actions", json={"kind": "HIDE"}
        )
        assert acted.status_code == 201, acted.text
        assert meta.hidden == []
    finally:
        await client.aclose()


async def test_a_refused_facebook_hide_records_no_action(app, meta):
    """An Action records what happened to the comment. If Facebook refuses,
    nothing happened, so no Action may remain."""
    meta.comments = [provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់")]
    client = await connected_client(app)
    try:
        await client.post("/api/v1/facebook/sync")
        meta.refuse = True

        failed = await client.post("/api/v1/comments/fb-c-1/actions", json={"kind": "HIDE"})
        assert failed.status_code == 502, failed.text

        listed = await client.get("/api/v1/comments", params={"query": "សេវាកម្មនេះយឺត"})
        assert listed.json()["items"][0]["latest_action"] is None
        async with database.acquire() as connection:
            rows = await connection.fetchval(
                "SELECT COUNT(*) FROM action WHERE comment_id = 'fb-c-1'"
            )
        assert rows == 0
    finally:
        await client.aclose()


async def test_sync_without_a_connected_page_is_refused(app, meta):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        created = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": f"nosync-{uuid.uuid4().hex[:10]}@example.com",
                "password": "a-long-enough-password",
                "display_name": "Sok Dara",
                "organization": "No Page",
            },
        )
        client.headers["Authorization"] = f"Bearer {created.json()['token']}"
        refused = await client.post("/api/v1/facebook/sync")
        assert refused.status_code == 409
    finally:
        await client.aclose()


async def test_sync_requires_authentication(app):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        assert (await client.post("/api/v1/facebook/sync")).status_code == 401
    finally:
        await client.aclose()


async def test_removing_samples_keeps_comments_imported_from_the_page(app, meta):
    """The delete is scoped by the sample Page id, so a real imported comment
    must survive it. Losing those would destroy the actual moderation record."""
    meta.comments = [provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់")]
    client = await connected_client(app)
    try:
        await client.post("/api/v1/facebook/sync")
        workspace_id = (await client.get("/api/v1/settings")).json()["workspace_id"]
        async with database.acquire() as connection:
            stored = await connection.fetchval(
                "SELECT COUNT(*) FROM comment_content "
                "WHERE workspace_id = $1 AND page_id = 'demo-page'",
                workspace_id,
            )
        assert stored > 0, "expected seeded samples to exist alongside the imported comment"

        removed = await client.request("DELETE", "/api/v1/comments/samples")
        assert removed.status_code == 200, removed.text
        assert removed.json()["removed"] > 0

        after = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        assert [item["comment_id"] for item in after["items"]] == ["fb-c-1"]
        assert after["total"] == 1
    finally:
        await client.aclose()


async def test_removing_samples_twice_is_harmless(app, meta):
    client = await connected_client(app)
    try:
        first = await client.request("DELETE", "/api/v1/comments/samples")
        assert first.json()["removed"] > 0
        second = await client.request("DELETE", "/api/v1/comments/samples")
        assert second.status_code == 200
        assert second.json()["removed"] == 0
    finally:
        await client.aclose()


async def test_a_member_cannot_empty_the_shared_workspace(app, meta):
    """Emptying a workspace affects everyone in it, so it is an owner action."""
    owner = await connected_client(app)
    member = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        invitation = await owner.post("/api/v1/team/invitations", json={"role": "member"})
        assert invitation.status_code == 201, invitation.text
        token = invitation.json()["token"]

        created = await member.post(
            "/api/v1/auth/signup",
            json={
                "email": f"member-{uuid.uuid4().hex[:10]}@example.com",
                "password": "a-long-enough-password",
                "display_name": "Sok Dara",
                "organization": "Joining",
            },
        )
        member.headers["Authorization"] = f"Bearer {created.json()['token']}"
        joined = await member.post(f"/api/v1/team/invitations/{token}/accept")
        assert joined.status_code == 200, joined.text

        refused = await member.request("DELETE", "/api/v1/comments/samples")
        assert refused.status_code == 403

        workspace_id = (await owner.get("/api/v1/settings")).json()["workspace_id"]
        async with database.acquire() as connection:
            remaining = await connection.fetchval(
                "SELECT COUNT(*) FROM comment_content "
                "WHERE workspace_id = $1 AND page_id = 'demo-page'",
                workspace_id,
            )
        assert remaining > 0
    finally:
        await owner.aclose()
        await member.aclose()


async def test_a_connected_workspace_lists_only_its_own_page(app, meta):
    """Samples exist so the screens are not empty before a Page is connected.
    Once one is, mixing them into the queue makes it untrustworthy — a
    moderator cannot tell which rows are real."""
    meta.comments = [provider_comment("fb-c-1", "សេវាកម្មនេះយឺតណាស់")]
    client = await connected_client(app)
    try:
        await client.post("/api/v1/facebook/sync")

        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        assert [item["comment_id"] for item in listed["items"]] == ["fb-c-1"]
        assert listed["total"] == 1

        # The counts must agree with the list rather than counting hidden rows.
        summary = (await client.get("/api/v1/comments/summary")).json()
        assert summary["processed"] == 1
    finally:
        await client.aclose()


async def test_a_workspace_with_no_connection_still_sees_its_samples(app, meta):
    """Filtering unconditionally would leave a new workspace with an empty
    dashboard and nothing to demonstrate."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        created = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": f"nopage-{uuid.uuid4().hex[:10]}@example.com",
                "password": "a-long-enough-password",
                "display_name": "Sok Dara",
                "organization": "No Page Yet",
            },
        )
        client.headers["Authorization"] = f"Bearer {created.json()['token']}"

        listed = (await client.get("/api/v1/comments", params={"limit": 100})).json()
        assert listed["total"] > 0
        summary = (await client.get("/api/v1/comments/summary")).json()
        assert summary["processed"] == listed["total"]
    finally:
        await client.aclose()
