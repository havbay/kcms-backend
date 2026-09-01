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
