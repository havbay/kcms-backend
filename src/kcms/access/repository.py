"""Page connection requests, and the decisions made on them."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

# Everything an administrator is allowed to see. Comment content is absent by
# construction rather than by filtering, so a later column addition cannot
# quietly leak it into an administration view.
ADMIN_FIELDS = """
    r.id, r.workspace_id, r.page_name, r.monthly_comments, r.team_size,
    r.note, r.status, r.decision_reason, r.decided_at, r.created_at,
    w.name AS workspace_name,
    u.display_name AS requester_name,
    i.provider_id AS requester_email
"""


async def create_request(
    connection: asyncpg.Connection,
    workspace_id: str,
    user_id: str,
    page_name: str,
    monthly_comments: str,
    team_size: str,
    note: str | None,
) -> dict[str, Any]:
    """Submit a request, replacing any still open for this workspace.

    Replacing rather than queueing keeps one impatient client from filling the
    administrator's queue with duplicates of the same ask.
    """
    async with connection.transaction():
        await connection.execute(
            "DELETE FROM access_request WHERE workspace_id = $1 AND status = 'PENDING'",
            workspace_id,
        )
        row = await connection.fetchrow(
            """INSERT INTO access_request
               (id, workspace_id, requested_by, page_name, monthly_comments, team_size, note)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id, workspace_id, page_name, monthly_comments, team_size,
                         note, status, decision_reason, decided_at, created_at""",
            uuid.uuid4().hex, workspace_id, user_id, page_name.strip(),
            monthly_comments, team_size, (note or "").strip() or None,
        )
    return dict(row)


async def latest_for_workspace(
    connection: asyncpg.Connection, workspace_id: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """SELECT id, workspace_id, page_name, monthly_comments, team_size, note,
                  status, decision_reason, decided_at, created_at
           FROM access_request WHERE workspace_id = $1
           ORDER BY created_at DESC LIMIT 1""",
        workspace_id,
    )
    return dict(row) if row else None


async def list_for_admin(
    connection: asyncpg.Connection, status: str | None = None
) -> list[dict[str, Any]]:
    query = f"""
        SELECT {ADMIN_FIELDS}
        FROM access_request r
        JOIN workspace w ON w.id = r.workspace_id
        JOIN app_user u ON u.id = r.requested_by
        LEFT JOIN identity i
          ON i.user_id = r.requested_by AND i.provider = 'email'
        {"WHERE r.status = $1" if status else ""}
        ORDER BY r.created_at DESC
    """
    rows = await connection.fetch(query, *( [status] if status else [] ))
    return [dict(row) for row in rows]


async def decide(
    connection: asyncpg.Connection,
    request_id: str,
    decision: str,
    reason: str | None,
    decided_by: str,
) -> dict[str, Any] | None:
    """Approve or decline. Returns None when the request is missing or already
    decided, so a second decision cannot silently overwrite the first."""
    async with connection.transaction():
        row = await connection.fetchrow(
            """UPDATE access_request
               SET status = $2, decision_reason = $3, decided_by = $4, decided_at = NOW()
               WHERE id = $1 AND status = 'PENDING'
               RETURNING id, workspace_id, page_name, monthly_comments, team_size,
                         note, status, decision_reason, decided_at, created_at""",
            request_id, decision, reason, decided_by,
        )
        if row is None:
            return None
        if decision == "APPROVED":
            # Approval lifts the sandbox restriction. It does not connect a
            # Page: Meta OAuth is a later slice.
            await connection.execute(
                "UPDATE workspace SET is_sandbox = FALSE WHERE id = $1", row["workspace_id"]
            )
    return dict(row)
