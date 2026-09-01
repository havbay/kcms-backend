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
        if token != "valid-page-token-for-test":
            raise ValueError("invalid Page token")
        return ProviderPage(
            page_id="page-123",
            page_name="Angkor Shop",
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
                "DELETE FROM page_connection WHERE external_page_id IN ('page-123', 'page-456')"
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
        empty = await client.get("/api/v1/facebook/connection")
        assert empty.status_code == 200
        assert empty.json()["state"] == "NOT_CONNECTED"

        await client.post(
            "/api/v1/facebook/connections/manual",
            json={"page_access_token": "valid-page-token-for-test"},
        )
        connected = await client.get("/api/v1/facebook/connection")
        assert connected.status_code == 200
        assert connected.json()["page_id"] == "page-123"
        assert "page_access_token" not in connected.json()
        assert "credential" not in connected.json()

        removed = await client.delete("/api/v1/facebook/connection")
        assert removed.status_code == 204
        assert (await client.get("/api/v1/facebook/connection")).json()["state"] == "NOT_CONNECTED"
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
        assert (await anonymous.get("/api/v1/facebook/connection")).status_code == 401
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
        assert query["scope"] == [
            "pages_show_list,pages_read_engagement,pages_manage_engagement"
        ]
    finally:
        await client.aclose()
