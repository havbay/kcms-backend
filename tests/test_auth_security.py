"""The Telegram hash check is the whole security of that provider.

If it can be bypassed, anyone can POST an arbitrary user id and be signed in
as them, so each bypass route gets its own test.
"""

import hashlib
import hmac
import time

import httpx
import pytest

from kcms.app import create_app
from kcms.auth.security import (
    hash_password,
    new_session_token,
    verify_password,
    verify_telegram_payload,
)

BOT_TOKEN = "123456:test-bot-token"


async def test_public_email_signup_is_disabled_by_default(monkeypatch):
    from kcms.settings import settings

    monkeypatch.setattr(settings, "public_signup_enabled", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": "visitor@example.com",
                "password": "a-long-enough-password",
                "display_name": "Visitor",
            },
        )

    assert response.status_code == 404


def sign(payload: dict[str, str], token: str = BOT_TOKEN) -> dict[str, str]:
    check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload) if k != "hash")
    secret = hashlib.sha256(token.encode()).digest()
    signed = dict(payload)
    signed["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return signed


def fresh_payload(**overrides: str) -> dict[str, str]:
    payload = {"id": "42", "first_name": "Sophea", "auth_date": str(int(time.time()))}
    payload.update(overrides)
    return payload


def test_password_round_trips_and_rejects_wrong_input():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("anything", None)
    assert not verify_password("anything", "not-a-valid-digest")


def test_the_same_password_hashes_differently_each_time():
    """A shared salt would make the digests a rainbow-table target."""
    assert hash_password("same") != hash_password("same")


def test_session_token_is_returned_once_and_stored_only_as_a_hash():
    token, stored = new_session_token()
    assert token != stored
    assert stored == hashlib.sha256(token.encode()).hexdigest()


def test_a_correctly_signed_telegram_payload_is_accepted():
    assert verify_telegram_payload(sign(fresh_payload()), BOT_TOKEN)


def test_forged_payload_without_a_valid_hash_is_rejected():
    forged = fresh_payload()
    forged["hash"] = "0" * 64
    assert not verify_telegram_payload(forged, BOT_TOKEN)


def test_payload_signed_with_a_different_bot_token_is_rejected():
    assert not verify_telegram_payload(sign(fresh_payload(), "999:someone-elses"), BOT_TOKEN)


def test_tampering_with_the_user_id_after_signing_is_rejected():
    """The attack that matters: sign as yourself, then swap in another id."""
    signed = sign(fresh_payload(id="42"))
    signed["id"] = "1"
    assert not verify_telegram_payload(signed, BOT_TOKEN)


def test_an_old_payload_is_rejected_so_captures_cannot_be_replayed():
    stale = sign(fresh_payload(auth_date=str(int(time.time()) - 3600)))
    assert not verify_telegram_payload(stale, BOT_TOKEN)


def test_a_future_dated_payload_is_rejected():
    ahead = sign(fresh_payload(auth_date=str(int(time.time()) + 3600)))
    assert not verify_telegram_payload(ahead, BOT_TOKEN)


@pytest.mark.parametrize("token", ["", None])
def test_verification_fails_closed_when_the_bot_is_not_configured(token):
    """Telegram sign-in must be impossible, not open, when unconfigured."""
    assert not verify_telegram_payload(sign(fresh_payload()), token or "")
