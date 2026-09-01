import pytest
from cryptography.fernet import Fernet

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
