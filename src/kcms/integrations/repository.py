import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from kcms.integrations.contracts import ProviderPage

_CONNECTION_FIELDS = (
    "external_page_id AS page_id, page_name, "
    "connection_method AS method, tasks, connected_at, last_synced_at"
)


class PageAlreadyConnected(Exception):
    """The Page is connected to a different workspace.

    A Page belongs to one workspace so two clients cannot moderate it at once.
    """


class PageLimitReached(Exception):
    """The workspace's plan does not allow another connected Page."""


async def add_page_connection(
    connection: asyncpg.Connection,
    *,
    workspace_id: str,
    user_id: str,
    page: ProviderPage,
    method: str,
    credential_ciphertext: str,
) -> dict[str, Any]:
    clash = await connection.fetchval(
        "SELECT workspace_id FROM page_connection "
        "WHERE external_page_id = $1 AND workspace_id <> $2",
        page.page_id,
        workspace_id,
    )
    if clash:
        raise PageAlreadyConnected(page.page_id)

    row = await connection.fetchrow(
        f"""INSERT INTO page_connection
           (id, workspace_id, external_page_id, page_name, connection_method,
            credential_ciphertext, tasks, connected_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (workspace_id, external_page_id) DO UPDATE SET
             page_name = EXCLUDED.page_name,
             connection_method = EXCLUDED.connection_method,
             credential_ciphertext = EXCLUDED.credential_ciphertext,
             tasks = EXCLUDED.tasks,
             connected_by = EXCLUDED.connected_by,
             connected_at = NOW(),
             last_synced_at = NULL,
             updated_at = NOW()
           RETURNING {_CONNECTION_FIELDS}""",
        uuid.uuid4().hex,
        workspace_id,
        page.page_id,
        page.page_name,
        method,
        credential_ciphertext,
        list(page.tasks),
        user_id,
    )
    return dict(row)


async def count_page_connections(connection: asyncpg.Connection, workspace_id: str) -> int:
    return await connection.fetchval(
        "SELECT COUNT(*) FROM page_connection WHERE workspace_id = $1", workspace_id
    )


async def list_page_connections(
    connection: asyncpg.Connection, workspace_id: str
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        f"""SELECT {_CONNECTION_FIELDS}
           FROM page_connection WHERE workspace_id = $1
           ORDER BY connected_at""",
        workspace_id,
    )
    return [dict(row) for row in rows]


async def get_page_connection(
    connection: asyncpg.Connection, workspace_id: str, page_id: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        f"""SELECT {_CONNECTION_FIELDS}
           FROM page_connection WHERE workspace_id = $1 AND external_page_id = $2""",
        workspace_id,
        page_id,
    )
    return dict(row) if row else None


async def delete_page_connection(
    connection: asyncpg.Connection, workspace_id: str, page_id: str
) -> bool:
    result = await connection.execute(
        "DELETE FROM page_connection WHERE workspace_id = $1 AND external_page_id = $2",
        workspace_id,
        page_id,
    )
    return result == "DELETE 1"


async def create_oauth_attempt(
    connection: asyncpg.Connection,
    *,
    state_hash: str,
    workspace_id: str,
    user_id: str,
) -> None:
    await connection.execute(
        "DELETE FROM facebook_oauth_attempt WHERE expires_at <= NOW()"
    )
    await connection.execute(
        """INSERT INTO facebook_oauth_attempt
           (state_hash, workspace_id, user_id, expires_at)
           VALUES ($1, $2, $3, $4)""",
        state_hash,
        workspace_id,
        user_id,
        datetime.now(UTC) + timedelta(minutes=10),
    )


async def oauth_attempt(
    connection: asyncpg.Connection,
    state_hash: str,
    *,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """SELECT state_hash, workspace_id, user_id, candidate_ciphertext, expires_at
           FROM facebook_oauth_attempt
           WHERE state_hash = $1 AND expires_at > NOW()
             AND ($2::TEXT IS NULL OR workspace_id = $2)
             AND ($3::TEXT IS NULL OR user_id = $3)""",
        state_hash,
        workspace_id,
        user_id,
    )
    return dict(row) if row else None


async def store_oauth_candidates(
    connection: asyncpg.Connection, state_hash: str, ciphertext: str
) -> bool:
    result = await connection.execute(
        """UPDATE facebook_oauth_attempt SET candidate_ciphertext = $2
           WHERE state_hash = $1 AND expires_at > NOW()""",
        state_hash,
        ciphertext,
    )
    return result == "UPDATE 1"


async def delete_oauth_attempt(connection: asyncpg.Connection, state_hash: str) -> None:
    await connection.execute(
        "DELETE FROM facebook_oauth_attempt WHERE state_hash = $1", state_hash
    )


async def mark_synced(connection: asyncpg.Connection, workspace_id: str, page_id: str) -> None:
    await connection.execute(
        "UPDATE page_connection SET last_synced_at = NOW(), updated_at = NOW() "
        "WHERE workspace_id = $1 AND external_page_id = $2",
        workspace_id,
        page_id,
    )


async def credential_for_page(
    connection: asyncpg.Connection, workspace_id: str, page_id: str
) -> dict[str, Any] | None:
    """The stored Page credential. Kept separate from get_page_connection so
    the ciphertext is read only where it is actually needed."""
    row = await connection.fetchrow(
        "SELECT external_page_id, credential_ciphertext, tasks "
        "FROM page_connection WHERE workspace_id = $1 AND external_page_id = $2",
        workspace_id,
        page_id,
    )
    return dict(row) if row else None
