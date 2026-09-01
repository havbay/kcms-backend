from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from kcms.settings import settings


class CredentialCipher(Protocol):
    def seal(self, value: str) -> str: ...
    def open(self, value: str) -> str: ...


class FernetCredentialCipher:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("stored provider credential could not be decrypted") from exc


def get_credential_cipher() -> CredentialCipher:
    if not settings.integration_encryption_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider credential encryption is not configured",
        )
    try:
        return FernetCredentialCipher(settings.integration_encryption_key)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider credential encryption is misconfigured",
        ) from exc


def get_optional_credential_cipher() -> CredentialCipher | None:
    try:
        return get_credential_cipher()
    except HTTPException:
        return None
