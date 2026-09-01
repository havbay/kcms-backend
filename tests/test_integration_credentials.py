import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from kcms.integrations.credentials import FernetCredentialCipher


def test_fernet_credential_cipher_round_trips_without_plaintext_storage():
    cipher = FernetCredentialCipher(Fernet.generate_key().decode())

    sealed = cipher.seal("page-access-token-secret")

    assert sealed != "page-access-token-secret"
    assert "page-access-token-secret" not in sealed
    assert cipher.open(sealed) == "page-access-token-secret"


def test_fernet_credential_cipher_rejects_tampered_ciphertext():
    cipher = FernetCredentialCipher(Fernet.generate_key().decode())

    with pytest.raises(ValueError, match="could not be decrypted"):
        cipher.open("not-a-valid-fernet-token")


def test_page_token_setup_does_not_require_facebook_login_settings(monkeypatch):
    """A deployment with only a Graph version must still be able to connect a
    Page by token. Requiring the Login settings here blocked the simplest
    setup path behind an OAuth app it never uses."""
    from kcms.integrations.facebook import get_meta_client
    from kcms.settings import settings

    monkeypatch.setattr(settings, "meta_graph_version", "v21.0")
    for absent in (
        "meta_app_id", "meta_app_secret", "meta_login_config_id", "meta_oauth_redirect_uri"
    ):
        monkeypatch.setattr(settings, absent, "")

    client = get_meta_client()
    assert client is not None
    # Facebook Login still fails closed rather than building a broken URL.
    with pytest.raises(HTTPException) as refused:
        client.authorization_url("state-value")
    assert refused.value.status_code == 503


class _StubResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _graph_client():
    from kcms.integrations.facebook import GraphMetaClient

    return GraphMetaClient("v21.0", "app", "secret", "https://kcms.test/cb", "scopes", "cfg")


async def test_a_user_token_is_refused_with_the_correction_to_make(monkeypatch):
    """A User token answers /me with an id and a name just like a Page token.
    Without the token type it would be stored as a Page and every later call
    would fail with nothing pointing at the real mistake."""
    client = _graph_client()

    async def fake_get(path, params):
        assert path == "debug_token"
        return {"data": {"type": "USER", "profile_id": None, "scopes": ["pages_show_list"]}}

    monkeypatch.setattr(client, "_get", fake_get)

    with pytest.raises(ValueError) as refused:
        await client.validate_page_token("user-token")
    assert "not a Page access token" in str(refused.value)


async def test_a_page_token_connects_without_permission_to_read_the_page(monkeypatch):
    """Reading the Page node needs pages_read_engagement, which a token can
    lack while still being able to read comments and hide them. Refusing there
    would reject a token that works."""
    client = _graph_client()

    async def fake_get(path, params):
        if path == "debug_token":
            return {
                "data": {
                    "type": "PAGE",
                    "profile_id": "page-1",
                    "scopes": ["pages_manage_engagement", "pages_read_user_content"],
                }
            }
        raise ValueError("Meta rejected the authorization")

    monkeypatch.setattr(client, "_get", fake_get)

    page = await client.validate_page_token("page-token")
    assert page.page_id == "page-1"
    # The permission that hiding actually requires is present, so the UI must
    # not claim this connection cannot moderate.
    assert page.can_moderate is True


async def test_a_readable_page_keeps_the_name_and_tasks_meta_reports(monkeypatch):
    client = _graph_client()

    async def fake_get(path, params):
        if path == "debug_token":
            return {"data": {"type": "PAGE", "profile_id": "page-1", "scopes": []}}
        return {"name": "KCMS-Demo", "tasks": ["MODERATE", "MANAGE"]}

    monkeypatch.setattr(client, "_get", fake_get)

    page = await client.validate_page_token("page-token")
    assert page.page_name == "KCMS-Demo"
    assert page.tasks == ("MODERATE", "MANAGE")


def test_bare_page_tasks_count_as_moderation_capability():
    """/me/accounts returns MODERATE and MANAGE rather than the PROFILE_PLUS_
    names. Recognising only the latter reported a real, moderatable Page as
    unable to moderate."""
    from kcms.integrations.contracts import ProviderPage

    page = ProviderPage(
        page_id="page-1",
        page_name="KCMS-Demo",
        access_token="t",
        tasks=("MODERATE", "MESSAGING", "ANALYZE", "ADVERTISE", "CREATE_CONTENT", "MANAGE"),
    )
    assert page.can_moderate is True

    without = ProviderPage(
        page_id="page-2", page_name="Other", access_token="t", tasks=("ANALYZE",)
    )
    assert without.can_moderate is False


async def test_sync_names_the_missing_read_permission(monkeypatch):
    """/feed answers {"data": []} rather than an error when the token lacks
    pages_read_engagement. Without this check an unusable connection reports
    "no new comments" forever and never says why."""
    client = _graph_client()

    async def fake_get(path, params):
        if path == "debug_token":
            return {"data": {"scopes": ["pages_manage_engagement", "pages_show_list"]}}
        raise AssertionError("the feed must not be read without the permission")

    monkeypatch.setattr(client, "_get", fake_get)

    with pytest.raises(ValueError) as refused:
        await client.fetch_comments("page-1", "page-token")
    assert "pages_read_engagement" in str(refused.value)


async def test_sync_reads_the_feed_once_the_permission_is_granted(monkeypatch):
    client = _graph_client()

    async def fake_get(path, params):
        if path == "debug_token":
            return {"data": {"scopes": ["pages_read_engagement"]}}
        return {
            "data": [
                {
                    "id": "post-1",
                    "message": "តើសេវាកម្មយើងយ៉ាងណា?",
                    "permalink_url": "https://facebook.com/p/1",
                    "comments": {
                        "data": [
                            {
                                "id": "c-1",
                                "message": "សេវាកម្មនេះយឺតណាស់",
                                "created_time": "2026-09-01T10:00:00+0000",
                            }
                        ]
                    },
                }
            ]
        }

    monkeypatch.setattr(client, "_get", fake_get)

    comments = await client.fetch_comments("page-1", "page-token")
    assert [c.comment_id for c in comments] == ["c-1"]
    assert comments[0].post_text == "តើសេវាកម្មយើងយ៉ាងណា?"
    # Meta withholds `from` for commenters who have not authorized the app.
    assert comments[0].author_ref == "fb:c-1"
