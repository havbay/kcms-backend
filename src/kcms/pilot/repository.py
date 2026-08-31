from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from kcms.auth import repository as auth_repository
from kcms.auth.security import hash_session_token

INVITATION_DAYS = 7


async def create_request(
    connection: asyncpg.Connection,
    *,
    name: str,
    organization: str,
    email: str,
    facebook_page: str,
    note: str | None,
) -> dict[str, Any]:
    email = email.strip().lower()
    async with connection.transaction():
        await connection.execute(
            "DELETE FROM pilot_request WHERE LOWER(email) = $1 AND status = 'PENDING'", email
        )
        row = await connection.fetchrow(
            """INSERT INTO pilot_request
               (id, name, organization, email, facebook_page, note)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id, status""",
            uuid.uuid4().hex,
            name.strip(),
            organization.strip(),
            email,
            facebook_page.strip(),
            (note or "").strip() or None,
        )
    return dict(row)


async def list_for_admin(
    connection: asyncpg.Connection, status: str | None = None
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        f"""SELECT r.id, r.name, r.organization, r.email, r.facebook_page,
                    r.note, r.status, r.decision_reason, r.decided_at, r.created_at,
                    d.status AS delivery_status
             FROM pilot_request r
             LEFT JOIN LATERAL (
                 SELECT status FROM notification_delivery
                 WHERE entity_type = 'PILOT_REQUEST' AND entity_id = r.id
                 ORDER BY created_at DESC LIMIT 1
             ) d ON TRUE
             {"WHERE r.status = $1" if status else ""}
             ORDER BY r.created_at DESC""",
        *([status] if status else []),
    )
    return [dict(row) for row in rows]


async def decide(
    connection: asyncpg.Connection,
    *,
    request_id: str,
    decision: str,
    reason: str | None,
    admin_id: str,
) -> dict[str, Any] | None:
    """Commit the decision and account/workspace setup before email is sent."""
    async with connection.transaction():
        request = await connection.fetchrow(
            "SELECT * FROM pilot_request WHERE id = $1 AND status = 'PENDING' FOR UPDATE",
            request_id,
        )
        if not request:
            return None

        token: str | None = None
        workspace_id: str | None = None
        existing_user = False
        if decision == "APPROVED":
            user_id = await connection.fetchval(
                """SELECT user_id FROM identity
                   WHERE provider = 'email' AND provider_id = $1""",
                request["email"].strip().lower(),
            )
            if user_id:
                existing_user = True
                workspace = await auth_repository.workspace_for_user(connection, user_id)
                if workspace:
                    workspace_id = workspace["id"]
                    await connection.execute(
                        "UPDATE workspace SET is_sandbox = FALSE WHERE id = $1", workspace_id
                    )
            else:
                workspace_id = uuid.uuid4().hex
                await connection.execute(
                    "INSERT INTO workspace (id, name, is_sandbox) VALUES ($1, $2, FALSE)",
                    workspace_id,
                    request["organization"],
                )
                token = secrets.token_urlsafe(32)
                await connection.execute(
                    """INSERT INTO invitation
                       (token_hash, workspace_id, created_by, role, expires_at,
                        recipient_email, purpose)
                       VALUES ($1, $2, $3, 'owner', $4, $5, 'PILOT_SETUP')""",
                    hash_session_token(token),
                    workspace_id,
                    admin_id,
                    datetime.now(UTC) + timedelta(days=INVITATION_DAYS),
                    request["email"].strip().lower(),
                )

        row = await connection.fetchrow(
            """UPDATE pilot_request
               SET status = $2, decision_reason = $3, decided_by = $4,
                   decided_at = NOW(), workspace_id = $5
               WHERE id = $1
               RETURNING id, name, organization, email, facebook_page, note,
                         status, decision_reason, decided_at, created_at""",
            request_id,
            decision,
            reason,
            admin_id,
            workspace_id,
        )
    return {**dict(row), "token": token, "existing_user": existing_user}


async def record_delivery(
    connection: asyncpg.Connection,
    *,
    request_id: str,
    recipient: str,
    kind: str,
    status: str,
    detail: str | None,
) -> None:
    await connection.execute(
        """INSERT INTO notification_delivery
           (entity_type, entity_id, recipient, kind, status, detail)
           VALUES ('PILOT_REQUEST', $1, $2, $3, $4, $5)""",
        request_id,
        recipient,
        kind,
        status,
        detail,
    )


async def preview_setup_invitation(
    connection: asyncpg.Connection, token: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """SELECT w.name AS organization, i.recipient_email AS email, i.expires_at
           FROM invitation i JOIN workspace w ON w.id = i.workspace_id
           WHERE i.token_hash = $1 AND i.purpose = 'PILOT_SETUP'
             AND i.accepted_by IS NULL AND i.expires_at > NOW()""",
        hash_session_token(token),
    )
    return dict(row) if row else None


async def accept_setup_invitation(
    connection: asyncpg.Connection,
    *,
    token: str,
    display_name: str,
    password: str,
) -> tuple[str, dict[str, Any]] | None:
    token_hash = hash_session_token(token)
    async with connection.transaction():
        invitation = await connection.fetchrow(
            """SELECT i.workspace_id, i.recipient_email, w.name AS organization
               FROM invitation i JOIN workspace w ON w.id = i.workspace_id
               WHERE i.token_hash = $1 AND i.purpose = 'PILOT_SETUP'
                 AND i.accepted_by IS NULL AND i.expires_at > NOW()
               FOR UPDATE""",
            token_hash,
        )
        if not invitation:
            return None
        registered = await auth_repository.register_invited_email(
            connection,
            invitation["recipient_email"],
            password,
            display_name,
            invitation["organization"],
        )
        if not registered:
            return None
        session_token, user = registered
        await connection.execute(
            """INSERT INTO membership (workspace_id, user_id, role)
               VALUES ($1, $2, 'owner')""",
            invitation["workspace_id"],
            user["id"],
        )
        await connection.execute(
            """UPDATE invitation SET accepted_by = $2, accepted_at = NOW()
               WHERE token_hash = $1""",
            token_hash,
            user["id"],
        )
    return session_token, user
