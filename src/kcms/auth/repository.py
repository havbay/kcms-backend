"""Accounts, identities and sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from kcms.auth.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    verify_password,
)
from kcms.moderation.repository import seed_workspace
from kcms.settings import settings

SESSION_DAYS = 30
TRIAL_DAYS = 7


def trial_expired(workspace: dict[str, Any], now: datetime | None = None) -> bool:
    """Return whether this workspace's time-limited trial has ended."""
    if workspace.get("plan") != "TRIAL":
        return False
    expires_at = workspace.get("trial_expires_at")
    if expires_at is None:
        return False
    current = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= current


async def _issue_session(connection: asyncpg.Connection, user_id: str) -> str:
    token, token_hash = new_session_token()
    await connection.execute(
        "INSERT INTO session (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        token_hash, user_id, datetime.now(UTC) + timedelta(days=SESSION_DAYS),
    )
    return token


async def _create_user(
    connection: asyncpg.Connection, display_name: str, organization: str | None = None
) -> str:
    user_id = uuid.uuid4().hex
    await connection.execute(
        "INSERT INTO app_user (id, display_name, organization) VALUES ($1, $2, $3)",
        user_id, display_name, organization or None,
    )
    await _create_sandbox_workspace(connection, user_id, organization or display_name)
    return user_id


async def _create_sandbox_workspace(
    connection: asyncpg.Connection, user_id: str, name: str
) -> str:
    """Every account owns a workspace from the moment it exists, so no code
    path has to cope with a user who belongs nowhere."""
    workspace_id = uuid.uuid4().hex
    await connection.execute(
        """INSERT INTO workspace
           (id, name, is_sandbox, plan, trial_started_at, trial_expires_at)
           VALUES ($1, $2, $3, 'TRIAL', $4, $5)""",
        workspace_id,
        name.strip() or "My workspace",
        settings.public_signup_enabled,
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(days=TRIAL_DAYS),
    )
    await connection.execute(
        "INSERT INTO membership (workspace_id, user_id, role) VALUES ($1, $2, 'owner')",
        workspace_id, user_id,
    )
    # Legacy direct-signup tests explicitly enable this setting and retain the
    # scripted fixtures needed to exercise moderation behavior. Clerk-created
    # trial workspaces are always empty and receive only real Page comments.
    if settings.public_signup_enabled:
        await seed_workspace(connection, workspace_id)
    return workspace_id


async def workspace_for_user(
    connection: asyncpg.Connection, user_id: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """SELECT w.id, w.name, w.is_sandbox, w.plan, w.trial_started_at,
                         w.trial_expires_at, w.auto_delete_delay_minutes,
                  w.auto_hide_offensive, w.keyword_allowlist, w.keyword_blocklist, m.role
           FROM membership m JOIN workspace w ON w.id = m.workspace_id
           WHERE m.user_id = $1
           -- A joined team workspace wins over the personal sandbox, so
           -- accepting an invitation actually lands the person in that team.
           ORDER BY (w.id = m.workspace_id AND m.role = 'owner' AND w.is_sandbox) ASC,
                    m.created_at DESC
           LIMIT 1""",
        user_id,
    )
    return dict(row) if row else None


async def sign_up_with_email(
    connection: asyncpg.Connection,
    email: str,
    password: str,
    display_name: str,
    organization: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Returns (token, user), or None when the email is already registered."""
    email = email.strip().lower()
    async with connection.transaction():
        taken = await connection.fetchval(
            "SELECT 1 FROM identity WHERE provider = 'email' AND provider_id = $1", email
        )
        if taken:
            return None
        user_id = await _create_user(
            connection, display_name.strip() or email.split("@")[0], organization
        )
        is_admin = await _sync_platform_admin(connection, user_id, email)
        await connection.execute(
            """INSERT INTO identity (user_id, provider, provider_id, secret)
               VALUES ($1, 'email', $2, $3)""",
            user_id, email, hash_password(password),
        )
        token = await _issue_session(connection, user_id)
    return token, {
        "id": user_id,
        "display_name": display_name,
        "provider": "email",
        "is_platform_admin": is_admin,
    }


async def register_invited_email(
    connection: asyncpg.Connection,
    email: str,
    password: str,
    display_name: str,
    organization: str,
) -> tuple[str, dict[str, Any]] | None:
    """Create an account for a verified one-time onboarding invitation.

    Unlike open sign-up this deliberately does not create a sandbox workspace;
    the caller adds the person to the approved workspace in the same database
    transaction.
    """
    email = email.strip().lower()
    if await connection.fetchval(
        "SELECT 1 FROM identity WHERE provider = 'email' AND provider_id = $1", email
    ):
        return None
    user_id = uuid.uuid4().hex
    name = display_name.strip() or email.split("@", 1)[0]
    await connection.execute(
        "INSERT INTO app_user (id, display_name, organization) VALUES ($1, $2, $3)",
        user_id,
        name,
        organization.strip() or None,
    )
    await connection.execute(
        """INSERT INTO identity (user_id, provider, provider_id, secret)
           VALUES ($1, 'email', $2, $3)""",
        user_id,
        email,
        hash_password(password),
    )
    token = await _issue_session(connection, user_id)
    return token, {
        "id": user_id,
        "display_name": name,
        "provider": "email",
        "is_platform_admin": False,
    }


async def _sync_platform_admin(connection: asyncpg.Connection, user_id: str, email: str) -> bool:
    """Grant or revoke Platform Administration from the environment allowlist.

    Reconciled on every sign-in so removing an address from the allowlist
    actually takes the role away, rather than leaving it granted forever.
    """
    should_be_admin = email.strip().lower() in settings.platform_admin_email_set
    await connection.execute(
        "UPDATE app_user SET is_platform_admin = $2 WHERE id = $1", user_id, should_be_admin
    )
    return should_be_admin


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
    is_admin = await _sync_platform_admin(connection, row["id"], email)
    token = await _issue_session(connection, row["id"])
    return token, {
        "id": row["id"],
        "display_name": row["display_name"],
        "provider": "email",
        "is_platform_admin": is_admin,
    }


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


async def sign_in_with_clerk(
    connection: asyncpg.Connection,
    clerk_user_id: str,
    email: str | None,
    display_name: str,
) -> tuple[str, dict[str, Any]]:
    """Link a verified Clerk identity and provision its first trial workspace."""
    async with connection.transaction():
        row = await connection.fetchrow(
            """SELECT u.id, u.display_name, u.is_platform_admin
               FROM identity i JOIN app_user u ON u.id = i.user_id
               WHERE i.provider = 'clerk' AND i.provider_id = $1""",
            clerk_user_id,
        )
        if row:
            user_id, name = row["id"], row["display_name"]
            is_admin = bool(row["is_platform_admin"])
        else:
            existing_id = None
            if email:
                existing_id = await connection.fetchval(
                    "SELECT user_id FROM identity WHERE provider = 'email' AND provider_id = $1",
                    email.strip().lower(),
                )
            user_id = existing_id or await _create_user(connection, display_name)
            name = display_name if not existing_id else await connection.fetchval(
                "SELECT display_name FROM app_user WHERE id = $1", user_id
            )
            await connection.execute(
                """INSERT INTO identity (user_id, provider, provider_id, secret)
                   VALUES ($1, 'clerk', $2, NULL)""",
                user_id,
                clerk_user_id,
            )
            is_admin = False
        token = await _issue_session(connection, user_id)
    return token, {
        "id": user_id,
        "display_name": name,
        "provider": "clerk",
        "is_platform_admin": is_admin,
    }


async def user_for_token(connection: asyncpg.Connection, token: str) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """SELECT u.id, u.display_name, u.is_platform_admin FROM session s
           JOIN app_user u ON u.id = s.user_id
           WHERE s.token_hash = $1 AND s.expires_at > NOW()""",
        hash_session_token(token),
    )
    return dict(row) if row else None


async def revoke_session(connection: asyncpg.Connection, token: str) -> None:
    await connection.execute(
        "DELETE FROM session WHERE token_hash = $1", hash_session_token(token)
    )
