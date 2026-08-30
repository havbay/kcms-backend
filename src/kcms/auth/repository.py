"""Accounts, identities and sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from kcms.auth.security import hash_password, hash_session_token, new_session_token, verify_password

SESSION_DAYS = 30


async def _issue_session(connection: asyncpg.Connection, user_id: str) -> str:
    token, token_hash = new_session_token()
    await connection.execute(
        "INSERT INTO session (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        token_hash, user_id, datetime.now(UTC) + timedelta(days=SESSION_DAYS),
    )
    return token


async def _create_user(connection: asyncpg.Connection, display_name: str) -> str:
    user_id = uuid.uuid4().hex
    await connection.execute(
        "INSERT INTO app_user (id, display_name) VALUES ($1, $2)", user_id, display_name
    )
    return user_id


async def sign_up_with_email(
    connection: asyncpg.Connection, email: str, password: str, display_name: str
) -> tuple[str, dict[str, Any]] | None:
    """Returns (token, user), or None when the email is already registered."""
    email = email.strip().lower()
    async with connection.transaction():
        taken = await connection.fetchval(
            "SELECT 1 FROM identity WHERE provider = 'email' AND provider_id = $1", email
        )
        if taken:
            return None
        user_id = await _create_user(connection, display_name.strip() or email.split("@")[0])
        await connection.execute(
            """INSERT INTO identity (user_id, provider, provider_id, secret)
               VALUES ($1, 'email', $2, $3)""",
            user_id, email, hash_password(password),
        )
        token = await _issue_session(connection, user_id)
    return token, {"id": user_id, "display_name": display_name, "provider": "email"}


async def sign_in_with_email(
    connection: asyncpg.Connection, email: str, password: str
) -> tuple[str, dict[str, Any]] | None:
    row = await connection.fetchrow(
        """SELECT i.secret, u.id, u.display_name
           FROM identity i JOIN app_user u ON u.id = i.user_id
           WHERE i.provider = 'email' AND i.provider_id = $1""",
        email.strip().lower(),
    )
    # verify_password on a None secret still runs the same rejection path, so
    # an unknown email and a wrong password are not trivially distinguishable.
    if not row or not verify_password(password, row["secret"]):
        return None
    token = await _issue_session(connection, row["id"])
    return token, {"id": row["id"], "display_name": row["display_name"], "provider": "email"}


async def sign_in_with_telegram(
    connection: asyncpg.Connection, telegram_id: str, display_name: str
) -> tuple[str, dict[str, Any]]:
    """Find or create the account behind a verified Telegram identity.

    Only ever called after the payload signature has been checked.
    """
    async with connection.transaction():
        row = await connection.fetchrow(
            """SELECT u.id, u.display_name FROM identity i
               JOIN app_user u ON u.id = i.user_id
               WHERE i.provider = 'telegram' AND i.provider_id = $1""",
            telegram_id,
        )
        if row:
            user_id, name = row["id"], row["display_name"]
        else:
            user_id = await _create_user(connection, display_name)
            name = display_name
            await connection.execute(
                """INSERT INTO identity (user_id, provider, provider_id, secret)
                   VALUES ($1, 'telegram', $2, NULL)""",
                user_id, telegram_id,
            )
        token = await _issue_session(connection, user_id)
    return token, {"id": user_id, "display_name": name, "provider": "telegram"}


async def user_for_token(connection: asyncpg.Connection, token: str) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """SELECT u.id, u.display_name FROM session s
           JOIN app_user u ON u.id = s.user_id
           WHERE s.token_hash = $1 AND s.expires_at > NOW()""",
        hash_session_token(token),
    )
    return dict(row) if row else None


async def revoke_session(connection: asyncpg.Connection, token: str) -> None:
    await connection.execute(
        "DELETE FROM session WHERE token_hash = $1", hash_session_token(token)
    )
