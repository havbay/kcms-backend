"""Password hashing, session tokens, and Telegram payload verification."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping

# scrypt is in the standard library, so no extra dependency is pulled in for
# the one thing that must not be got wrong.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, digest_hex = stored.split("$", 1)
    try:
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except ValueError:
        return False
    # Constant-time: a timing difference here leaks whether a prefix matched.
    return hmac.compare_digest(candidate.hex(), digest_hex)


def new_session_token() -> tuple[str, str]:
    """Return (token_for_the_client, hash_to_store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_session_token(token)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


TELEGRAM_MAX_AGE_SECONDS = 300


def verify_telegram_payload(
    payload: Mapping[str, str], bot_token: str, now: float | None = None
) -> bool:
    """Verify a Telegram Login Widget payload.

    Without this check anyone can POST {"id": "1"} and be signed in as that
    user, so it is the whole of the security for this provider.
    """
    supplied_hash = payload.get("hash")
    if not supplied_hash or not bot_token:
        return False

    # auth_date bounds replay: a captured payload stays valid forever otherwise.
    try:
        auth_date = int(payload.get("auth_date", "0"))
    except ValueError:
        return False
    current = time.time() if now is None else now
    if current - auth_date > TELEGRAM_MAX_AGE_SECONDS or auth_date - current > 60:
        return False

    check_string = "\n".join(
        f"{key}={payload[key]}" for key in sorted(payload) if key != "hash"
    )
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied_hash)
