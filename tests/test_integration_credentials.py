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
    """A User token also answers /me with an id and a name. Without the node
    type it would be stored as though it were a Page, and every later call
    would fail with nothing pointing at the real mistake."""
    client = _graph_client()

    async def fake_get(path, params):
        return {"id": "99", "name": "Nara Chhuon", "metadata": {"type": "user"}}

    monkeypatch.setattr(client, "_get", fake_get)

    with pytest.raises(ValueError) as refused:
        await client.validate_page_token("user-token")
    assert "Page access token" in str(refused.value)


async def test_a_page_token_connects_even_when_tasks_cannot_be_read(monkeypatch):
    """Some Page tokens cannot read `tasks`. Refusing the whole connection over
    a missing capability list would block a Page that is otherwise usable."""
    client = _graph_client()
    calls: list[dict] = []

    async def fake_get(path, params):
        calls.append(params)
        if "tasks" in params["fields"]:
            raise ValueError("Meta rejected the authorization")
        return {"id": "page-1", "name": "KCMS-Demo", "metadata": {"type": "page"}}

    monkeypatch.setattr(client, "_get", fake_get)

    page = await client.validate_page_token("page-token")
    assert page.page_id == "page-1"
    assert page.page_name == "KCMS-Demo"
    assert page.tasks == ()
    assert len(calls) == 2
