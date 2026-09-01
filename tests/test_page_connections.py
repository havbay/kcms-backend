import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from asgi_lifespan import LifespanManager

from kcms.app import create_app
from kcms.integrations.contracts import ProviderPage
from kcms.integrations.credentials import get_credential_cipher
from kcms.integrations.facebook import GraphMetaClient, get_meta_client
from kcms.shared.database import database


class TestCipher:
    def seal(self, value: str) -> str:
        return f"sealed::{value[::-1]}"

    def open(self, value: str) -> str:
        assert value.startswith("sealed::")
        return value.removeprefix("sealed::")[::-1]


class FakeMetaClient:
    async def validate_page_token(self, token: str) -> ProviderPage:
        if not token.startswith("valid-page-token-for-test"):
            raise ValueError("invalid Page token")
        # A trailing suffix (e.g. "valid-page-token-for-test-2") stands in for
        # a distinct real Page, so a test can connect more than one.
        suffix = token.removeprefix("valid-page-token-for-test")
        return ProviderPage(
            page_id=f"page-123{suffix}",
            page_name=f"Angkor Shop{suffix}",
            access_token=token,
            tasks=("PROFILE_PLUS_MODERATE", "PROFILE_PLUS_MANAGE"),
        )

    def authorization_url(self, state: str) -> str:
        return f"https://facebook.example/authorize?state={state}"

    async def exchange_code(self, code: str) -> str:
        if code != "valid-code":
            raise ValueError("Meta rejected the authorization code")
        return "temporary-user-token"

    async def list_pages(self, user_token: str) -> list[ProviderPage]:
        assert user_token == "temporary-user-token"
        return [
            ProviderPage(
                page_id="page-456",
                page_name="Bayon News",
                access_token="oauth-page-token-secret",
                tasks=("PROFILE_PLUS_MODERATE",),
            )
        ]


async def approved_client(app) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    created = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"page-owner-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-long-enough-password",
            "display_name": "Dara Sok",
            "organization": "Angkor Shop",
        },
    )
    assert created.status_code == 201, created.text
    client.headers["Authorization"] = f"Bearer {created.json()['token']}"
    workspace_id = (await client.get("/api/v1/settings")).json()["workspace_id"]
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE workspace SET is_sandbox = FALSE WHERE id = $1", workspace_id
        )
    return client


@pytest.fixture
async def app():
    application = create_app()
    application.dependency_overrides[get_meta_client] = lambda: FakeMetaClient()
    application.dependency_overrides[get_credential_cipher] = lambda: TestCipher()
    async with LifespanManager(application):
        if not await database.is_reachable():
            pytest.skip("no database available")
        async with database.acquire() as connection:
            await connection.execute(
                "DELETE FROM page_connection "
                "WHERE external_page_id LIKE 'page-123%' OR external_page_id = 'page-456'"
            )
        yield application


async def test_manual_page_token_creates_one_non_disclosing_connection(app):
    client = await approved_client(app)
    try:
        response = await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body == {
            "state": "CONNECTED",
            "page_id": "page-123",
            "page_name": "Angkor Shop",
            "method": "MANUAL_TOKEN",
            "tasks": ["PROFILE_PLUS_MODERATE", "PROFILE_PLUS_MANAGE"],
            "can_moderate": True,
            "connected_at": body["connected_at"],
            "last_synced_at": None,
        }
        assert "page_access_token" not in body
        assert "credential" not in body

        workspace_id = (await client.get("/api/v1/settings")).json()["workspace_id"]
        async with database.acquire() as connection:
            stored = await connection.fetchrow(
                "SELECT credential_ciphertext FROM page_connection WHERE workspace_id = $1",
                workspace_id,
            )
        assert stored is not None
        assert stored["credential_ciphertext"] != "valid-page-token-for-test"
        assert "valid-page-token-for-test" not in stored["credential_ciphertext"]
    finally:
        await client.aclose()


async def test_connection_status_and_disconnect_never_return_the_credential(app):
    client = await approved_client(app)
    try:
        empty = await client.get("/api/v1/facebook/connections")
        assert empty.status_code == 200
        assert empty.json() == {"plan": "STARTER", "page_limit": 3, "connections": []}

        await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        connected = await client.get("/api/v1/facebook/connections")
        assert connected.status_code == 200
        assert connected.json()["connections"][0]["page_id"] == "page-123"
        assert "page_access_token" not in connected.text
        assert "credential" not in connected.text

        removed = await client.delete("/api/v1/facebook/connections/page-123")
        assert removed.status_code == 204
        assert (await client.get("/api/v1/facebook/connections")).json()["connections"] == []
    finally:
        await client.aclose()


async def test_invalid_token_is_rejected_and_a_client_sandbox_can_start_facebook(app):
    approved = await approved_client(app)
    sandbox = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        invalid = await approved.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "invalid-page-token-value"},
        )
        assert invalid.status_code == 422

        created = await sandbox.post(
            "/api/v1/auth/signup",
            json={
                "email": f"sandbox-{uuid.uuid4().hex[:10]}@example.com",
                "password": "a-long-enough-password",
                "display_name": "Sandbox User",
            },
        )
        sandbox.headers["Authorization"] = f"Bearer {created.json()['token']}"
        started = await sandbox.post("/api/v1/facebook/oauth/start")
        assert started.status_code == 201, started.text
        assert started.json()["authorization_url"].startswith(
            "https://facebook.example/authorize?state="
        )
    finally:
        await approved.aclose()
        await sandbox.aclose()

async def test_page_connections_require_a_session(app):
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    async with anonymous:
        assert (await anonymous.get("/api/v1/facebook/connections")).status_code == 401
        assert (
            await anonymous.post(
                "/api/v1/facebook/connections/manual",
                json={"page_access_token": "valid-page-token-for-test"},
            )
        ).status_code == 401


async def test_facebook_login_lists_pages_without_exposing_their_tokens(app):
    client = await approved_client(app)
    other = await approved_client(app)
    try:
        started = await client.post("/api/v1/facebook/oauth/start")
        assert started.status_code == 201, started.text
        authorization_url = started.json()["authorization_url"]
        state = parse_qs(urlparse(authorization_url).query)["state"][0]

        callback = await client.get(
            "/api/v1/facebook/oauth/callback",
            params={"code": "valid-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].endswith(f"/app/connect?facebook_session={state}")

        choices = await client.get(f"/api/v1/facebook/oauth/sessions/{state}")
        assert choices.status_code == 200
        assert choices.json() == {
            "pages": [
                {
                    "page_id": "page-456",
                    "page_name": "Bayon News",
                    "tasks": ["PROFILE_PLUS_MODERATE"],
                    "can_moderate": True,
                }
            ]
        }
        assert "oauth-page-token-secret" not in choices.text
        assert (
            await other.get(f"/api/v1/facebook/oauth/sessions/{state}")
        ).status_code == 404

        selected = await client.post(
            f"/api/v1/facebook/oauth/sessions/{state}/selection",
            json={"page_id": "page-456"},
        )
        assert selected.status_code == 201, selected.text
        assert selected.json()["method"] == "FACEBOOK_LOGIN"
        assert selected.json()["page_id"] == "page-456"
        assert "oauth-page-token-secret" not in selected.text
        assert (
            await client.post(
                f"/api/v1/facebook/oauth/sessions/{state}/selection",
                json={"page_id": "page-456"},
            )
        ).status_code == 404
    finally:
        await client.aclose()
        await other.aclose()


async def test_facebook_login_start_uses_the_business_login_configuration(app):
    app.dependency_overrides[get_meta_client] = lambda: GraphMetaClient(
        graph_version="v26.0",
        app_id="meta-app-123",
        app_secret="meta-secret-for-test",
        redirect_uri="https://api.example.com/api/v1/facebook/oauth/callback",
        scopes="pages_show_list,pages_read_engagement,pages_manage_engagement",
        login_config_id="business-login-config-456",
    )
    client = await approved_client(app)
    try:
        started = await client.post("/api/v1/facebook/oauth/start")

        assert started.status_code == 201, started.text
        query = parse_qs(urlparse(started.json()["authorization_url"]).query)
        assert query["config_id"] == ["business-login-config-456"]
        # A Business Login configuration carries its own permission set, so
        # scope is left out rather than sent alongside and ignored.
        assert "scope" not in query
        assert query["redirect_uri"] == [
            "https://api.example.com/api/v1/facebook/oauth/callback"
        ]
    finally:
        await client.aclose()


async def test_a_page_connected_elsewhere_is_refused_by_name(app):
    """A Page belongs to one workspace so two clients cannot moderate it at
    once. The upsert only resolves a conflict on workspace_id, so this
    collision used to surface as an unhandled unique violation and a 500."""
    first = await approved_client(app)
    second = await approved_client(app)
    try:
        connected = await first.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        assert connected.status_code == 201, connected.text

        refused = await second.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        assert refused.status_code == 409, refused.text
        assert "already connected" in refused.json()["detail"]

        # The first workspace keeps its connection untouched.
        still = await first.get("/api/v1/facebook/connections")
        assert still.json()["connections"][0]["page_id"] == "page-123"
    finally:
        await first.aclose()
        await second.aclose()


async def test_reconnecting_the_same_page_in_the_same_workspace_still_works(app):
    client = await approved_client(app)
    try:
        first = await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        assert first.status_code == 201
        again = await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        assert again.status_code == 201, again.text
    finally:
        await client.aclose()


async def test_a_starter_workspace_cannot_connect_a_fourth_page(app):
    """STARTER allows up to 3 Pages. A 4th must be refused until one is
    disconnected, and reconnecting an already-held Page must never count
    against the limit."""
    client = await approved_client(app)
    try:
        for suffix in ("-1", "-2", "-3"):
            connected = await client.post(
                "/api/v1/facebook/connections/manual",
                json={"page_access_token": f"valid-page-token-for-test{suffix}"},
            )
            assert connected.status_code == 201, connected.text

        # Reconnecting an already-held Page is not a new one, so it must not
        # be blocked by the limit.
        reconnected = await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test-1"},
        )
        assert reconnected.status_code == 201, reconnected.text

        refused = await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test-4"},
        )
        assert refused.status_code == 409, refused.text
        assert "plan" in refused.json()["detail"].lower()

        listed = (await client.get("/api/v1/facebook/connections")).json()
        assert listed["plan"] == "STARTER"
        assert listed["page_limit"] == 3
        assert len(listed["connections"]) == 3

        removed = await client.delete("/api/v1/facebook/connections/page-123-1")
        assert removed.status_code == 204

        now_fits = await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test-4"},
        )
        assert now_fits.status_code == 201, now_fits.text
    finally:
        await client.aclose()


async def test_a_cancelled_authorization_returns_to_kcms_with_a_reason(app):
    """Meta calls the callback with `error` and no `code` when someone cancels.
    That did not satisfy the signature, so the operator got a validation error
    on the API's own domain instead of coming back to the app."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = await client.get(
            "/api/v1/facebook/oauth/callback",
            params={"error": "access_denied", "error_reason": "user_denied"},
        )
        assert response.status_code == 303, response.text
        assert "/app/connect?facebook_error=denied" in response.headers["location"]
    finally:
        await client.aclose()


async def test_an_unknown_state_returns_to_kcms_rather_than_rendering_json(app):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = await client.get(
            "/api/v1/facebook/oauth/callback",
            params={"code": "valid-code", "state": "never-issued"},
        )
        assert response.status_code == 303
        assert "facebook_error=state_invalid" in response.headers["location"]
    finally:
        await client.aclose()


async def test_a_rejected_code_returns_to_kcms_with_a_reason(app):
    """The exchange failing is the most likely real failure, and it used to be
    entirely invisible: JSON on the API domain and no way back."""
    client = await approved_client(app)
    try:
        started = await client.post("/api/v1/facebook/oauth/start")
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

        response = await client.get(
            "/api/v1/facebook/oauth/callback",
            params={"code": "a-code-meta-will-reject", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "facebook_error=exchange_failed" in response.headers["location"]
    finally:
        await client.aclose()
