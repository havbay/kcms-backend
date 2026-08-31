"""Workspace membership, and the invitations that create it."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from kcms.auth.security import hash_session_token

INVITATION_DAYS = 7


async def list_members(connection: asyncpg.Connection, workspace_id: str) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT u.id AS user_id, u.display_name, m.role, m.created_at,
                  i.provider_id AS email
           FROM membership m
           JOIN app_user u ON u.id = m.user_id
           LEFT JOIN identity i ON i.user_id = u.id AND i.provider = 'email'
           WHERE m.workspace_id = $1
           ORDER BY (m.role = 'owner') DESC, m.created_at""",
        workspace_id,
    )
    return [dict(row) for row in rows]


async def count_owners(connection: asyncpg.Connection, workspace_id: str) -> int:
    return await connection.fetchval(
        "SELECT COUNT(*) FROM membership WHERE workspace_id = $1 AND role = 'owner'",
        workspace_id,
    )


async def create_invitation(
    connection: asyncpg.Connection, workspace_id: str, created_by: str, role: str
) -> tuple[str, dict[str, Any]]:
    """Returns (token_for_sharing, invitation). The token is shown once."""
    token = secrets.token_urlsafe(24)
    expires = datetime.now(UTC) + timedelta(days=INVITATION_DAYS)
    row = await connection.fetchrow(
        """INSERT INTO invitation (token_hash, workspace_id, created_by, role, expires_at)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING workspace_id, role, expires_at, created_at""",
        hash_session_token(token), workspace_id, created_by, role, expires,
    )
    return token, dict(row)


async def list_invitations(
    connection: asyncpg.Connection, workspace_id: str
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT token_hash, role, expires_at, created_at, accepted_at
           FROM invitation
           WHERE workspace_id = $1 AND accepted_by IS NULL AND expires_at > NOW()
           ORDER BY created_at DESC""",
        workspace_id,
    )
    return [dict(row) for row in rows]


async def revoke_invitation(
    connection: asyncpg.Connection, workspace_id: str, token_hash: str
) -> bool:
    result = await connection.execute(
        """DELETE FROM invitation
           WHERE workspace_id = $1 AND token_hash = $2 AND accepted_by IS NULL""",
        workspace_id, token_hash,
    )
    return result.endswith("1")


async def peek_invitation(connection: asyncpg.Connection, token: str) -> dict[str, Any] | None:
    """What an invitation offers, before anyone commits to accepting it."""
    row = await connection.fetchrow(
        """SELECT w.name AS workspace_name, i.role, i.expires_at
           FROM invitation i JOIN workspace w ON w.id = i.workspace_id
           WHERE i.token_hash = $1 AND i.accepted_by IS NULL AND i.expires_at > NOW()""",
        hash_session_token(token),
    )
    return dict(row) if row else None


async def accept_invitation(
    connection: asyncpg.Connection, token: str, user_id: str
) -> dict[str, Any] | None:
    """Join the workspace. Returns None when the invitation is spent, expired,
    or unknown, so a replayed link cannot add someone twice."""
    token_hash = hash_session_token(token)
    async with connection.transaction():
        row = await connection.fetchrow(
            """UPDATE invitation SET accepted_by = $2, accepted_at = NOW()
               WHERE token_hash = $1 AND accepted_by IS NULL AND expires_at > NOW()
               RETURNING workspace_id, role""",
            token_hash, user_id,
        )
        if row is None:
            return None
        await connection.execute(
            """INSERT INTO membership (workspace_id, user_id, role) VALUES ($1, $2, $3)
               ON CONFLICT (workspace_id, user_id) DO NOTHING""",
            row["workspace_id"], user_id, row["role"],
        )
        workspace = await connection.fetchrow(
            "SELECT id, name, is_sandbox FROM workspace WHERE id = $1", row["workspace_id"]
        )
    return dict(workspace) if workspace else None


async def remove_member(
    connection: asyncpg.Connection, workspace_id: str, user_id: str
) -> str | None:
    """Remove someone from the workspace.

    Refuses to remove the last owner: a workspace nobody can administer is
    unrecoverable through the product.
    """
    async with connection.transaction():
        role = await connection.fetchval(
            "SELECT role FROM membership WHERE workspace_id = $1 AND user_id = $2",
            workspace_id, user_id,
        )
        if role is None:
            return "NOT_FOUND"
        if role == "owner" and await count_owners(connection, workspace_id) <= 1:
            return "LAST_OWNER"
        await connection.execute(
            "DELETE FROM membership WHERE workspace_id = $1 AND user_id = $2",
            workspace_id, user_id,
        )
    return None
