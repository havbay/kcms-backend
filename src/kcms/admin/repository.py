"""Read-only platform administration queries.

These queries deliberately return workspace and integration metadata only. The
Platform Administrator can understand fleet health without receiving ordinary
customer comment text or provider credentials.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

_WORKSPACE_SUMMARY = """
SELECT
    w.id,
    w.name,
    w.plan,
    w.is_sandbox,
    w.is_suspended,
    w.trial_expires_at,
    CASE
        WHEN w.is_suspended THEN 'SUSPENDED'
        WHEN w.is_sandbox THEN 'SANDBOX'
        WHEN w.plan = 'TRIAL' AND w.trial_expires_at > NOW() THEN 'TRIAL'
        WHEN w.plan = 'TRIAL' THEN 'EXPIRED'
        ELSE 'ACTIVE'
    END AS status,
    w.created_at,
    (SELECT COUNT(*) FROM membership m WHERE m.workspace_id = w.id) AS member_count,
    (SELECT COUNT(*) FROM page_connection p WHERE p.workspace_id = w.id) AS page_count,
    (SELECT COUNT(*) FROM comment_content c WHERE c.workspace_id = w.id) AS comments_processed,
    (SELECT COUNT(*) FROM comment_content c
       WHERE c.workspace_id = w.id
         AND c.posted_at >= NOW() - INTERVAL '7 days') AS comments_processed_7d,
    (SELECT COUNT(*)
       FROM comment_content c
      WHERE c.workspace_id = w.id
        AND NOT EXISTS (
            SELECT 1 FROM action a WHERE a.comment_id = c.comment_id
        )
        AND EXISTS (
            SELECT 1 FROM verdict v
             WHERE v.comment_id = c.comment_id
               AND v.surfaced_reason <> 'cleared'
        )) AS pending_comments,
    (SELECT COUNT(*) FROM auto_reply_event e
       WHERE e.workspace_id = w.id
         AND e.occurred_at >= NOW() - INTERVAL '7 days'
         AND e.decision = 'replied'
         AND e.provider_applied = TRUE) AS replies_sent_7d,
    (SELECT COUNT(*) FROM auto_reply_event e
       WHERE e.workspace_id = w.id
         AND e.occurred_at >= NOW() - INTERVAL '7 days'
         AND e.decision = 'skipped'
         AND e.reason ILIKE 'Facebook reply failed:%') AS reply_failures_7d,
    w.auto_reply_enabled,
    (SELECT MAX(activity_at) FROM (
        SELECT MAX(p.updated_at) AS activity_at
          FROM page_connection p WHERE p.workspace_id = w.id
        UNION ALL
        SELECT MAX(c.posted_at) AS activity_at
          FROM comment_content c WHERE c.workspace_id = w.id
        UNION ALL
        SELECT MAX(e.occurred_at) AS activity_at
          FROM auto_reply_event e WHERE e.workspace_id = w.id
    ) activity) AS latest_activity_at
FROM workspace w
"""


async def list_workspaces(
    connection: asyncpg.Connection,
    *,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    search = f"%{query.strip()}%" if query and query.strip() else None
    rows = await connection.fetch(
        _WORKSPACE_SUMMARY
        + """
WHERE ($1::TEXT IS NULL OR w.name ILIKE $1 OR w.id ILIKE $1)
ORDER BY latest_activity_at DESC NULLS LAST, w.id
LIMIT $2 OFFSET $3
""",
        search,
        limit,
        offset,
    )
    return [dict(row) for row in rows]


async def get_workspace(
    connection: asyncpg.Connection, workspace_id: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        _WORKSPACE_SUMMARY + " WHERE w.id = $1",
        workspace_id,
    )
    return dict(row) if row else None


async def list_members(
    connection: asyncpg.Connection, workspace_id: str
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT u.id, u.display_name, m.role, m.created_at
           FROM membership m
           JOIN app_user u ON u.id = m.user_id
          WHERE m.workspace_id = $1
          ORDER BY CASE WHEN m.role = 'owner' THEN 0 ELSE 1 END,
                   m.created_at, u.id""",
        workspace_id,
    )
    return [dict(row) for row in rows]


async def list_pages(
    connection: asyncpg.Connection, workspace_id: str
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT external_page_id AS page_id, page_name, connection_method AS method,
                       tasks, connected_at, last_synced_at,
                       CASE
                           WHEN w.is_suspended THEN 'SUSPENDED'
                           WHEN last_synced_at IS NULL THEN 'NEVER_SYNCED'
                           WHEN last_synced_at < NOW() - INTERVAL '24 hours' THEN 'STALE'
                           ELSE 'HEALTHY'
                       END AS status
                  FROM page_connection
                  JOIN workspace w ON w.id = page_connection.workspace_id
                 WHERE page_connection.workspace_id = $1
                 ORDER BY connected_at DESC, external_page_id""",
        workspace_id,
    )
    return [dict(row) for row in rows]


async def overview(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(
        """SELECT
               COUNT(*) AS total_workspaces,
               COUNT(*) FILTER (WHERE status = 'ACTIVE') AS active_workspaces,
               COUNT(*) FILTER (WHERE status = 'TRIAL') AS trial_workspaces,
               COUNT(*) FILTER (WHERE status = 'EXPIRED') AS expired_workspaces,
               COUNT(*) FILTER (WHERE status = 'SUSPENDED') AS suspended_workspaces,
               (SELECT COUNT(*) FROM page_connection) AS connected_pages,
               (SELECT COUNT(*) FROM page_connection
                 WHERE last_synced_at IS NULL
                    OR last_synced_at < NOW() - INTERVAL '24 hours') AS integration_attention,
               (SELECT COUNT(*) FROM comment_content
                 WHERE posted_at >= NOW() - INTERVAL '7 days') AS comments_processed_7d,
               (SELECT COUNT(*) FROM auto_reply_event
                 WHERE occurred_at >= NOW() - INTERVAL '7 days'
                   AND decision = 'replied' AND provider_applied = TRUE) AS replies_sent_7d,
               (SELECT COUNT(*) FROM auto_reply_event
                 WHERE occurred_at >= NOW() - INTERVAL '7 days'
                   AND decision = 'skipped' AND reason ILIKE 'Facebook reply failed:%')
                   AS reply_failures_7d,
               (SELECT COUNT(*) FROM pilot_request WHERE status = 'PENDING')
                   AS pending_access_requests
          FROM (
               SELECT CASE
                       WHEN is_suspended THEN 'SUSPENDED'
                       WHEN is_sandbox THEN 'SANDBOX'
                       WHEN plan = 'TRIAL' AND trial_expires_at > NOW() THEN 'TRIAL'
                       WHEN plan = 'TRIAL' THEN 'EXPIRED'
                       ELSE 'ACTIVE'
                     END AS status
                FROM workspace
          ) states"""
    )
    return dict(row)


async def list_integrations(
    connection: asyncpg.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return Page connection health without returning provider credentials."""
    rows = await connection.fetch(
        """SELECT p.external_page_id AS page_id, p.page_name,
                      p.connection_method AS method, p.tasks,
                      p.connected_at, p.last_synced_at,
                      w.id AS workspace_id, w.name AS workspace_name,
                      CASE
                        WHEN w.is_suspended THEN 'SUSPENDED'
                        WHEN p.last_synced_at IS NULL THEN 'NEVER_SYNCED'
                        WHEN p.last_synced_at < NOW() - INTERVAL '24 hours' THEN 'STALE'
                        ELSE 'HEALTHY'
                      END AS status
                 FROM page_connection p
                 JOIN workspace w ON w.id = p.workspace_id
                WHERE ($1::TEXT IS NULL OR
                       CASE
                         WHEN w.is_suspended THEN 'SUSPENDED'
                         WHEN p.last_synced_at IS NULL THEN 'NEVER_SYNCED'
                         WHEN p.last_synced_at < NOW() - INTERVAL '24 hours' THEN 'STALE'
                         ELSE 'HEALTHY'
                       END = $1)
                ORDER BY CASE
                           WHEN w.is_suspended THEN 0
                           WHEN p.last_synced_at IS NULL THEN 1
                           WHEN p.last_synced_at < NOW() - INTERVAL '24 hours' THEN 2
                           ELSE 3
                         END,
                         p.page_name, p.external_page_id
                LIMIT $2 OFFSET $3""",
        status,
        limit,
        offset,
    )
    return [dict(row) for row in rows]


async def plan_counts(connection: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT plan, COUNT(*) AS workspace_count,
                      COUNT(*) FILTER (WHERE is_suspended) AS suspended_count
                 FROM workspace
                GROUP BY plan
                ORDER BY CASE plan WHEN 'TRIAL' THEN 0 WHEN 'STARTER' THEN 1 ELSE 2 END"""
    )
    return [dict(row) for row in rows]


async def list_platform_admins(connection: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT u.id, u.display_name, u.created_at,
                      COALESCE(
                        (SELECT i.provider_id FROM identity i
                          WHERE i.user_id = u.id AND i.provider = 'email'
                          ORDER BY i.created_at LIMIT 1),
                        'External identity'
                      ) AS email,
                      COUNT(s.token_hash) FILTER (WHERE s.expires_at > NOW())
                        AS active_session_count,
                      MAX(s.created_at) FILTER (WHERE s.expires_at > NOW())
                        AS latest_session_at
                 FROM app_user u
                 LEFT JOIN session s ON s.user_id = u.id
                WHERE u.is_platform_admin = TRUE
                GROUP BY u.id, u.display_name, u.created_at
                ORDER BY u.created_at, u.id"""
    )
    return [dict(row) for row in rows]


async def revoke_user_sessions(connection: asyncpg.Connection, user_id: str) -> int:
    result = await connection.execute("DELETE FROM session WHERE user_id = $1", user_id)
    return int(result.rsplit(" ", 1)[-1])


async def record_audit(
    connection: asyncpg.Connection,
    *,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    await connection.execute(
        """INSERT INTO platform_audit_event
           (actor_user_id, action, target_type, target_id, metadata)
           VALUES ($1, $2, $3, $4, $5::JSONB)""",
        actor_user_id,
        action,
        target_type,
        target_id,
        json.dumps(metadata or {}, ensure_ascii=False),
    )


async def list_audit_events(
    connection: asyncpg.Connection, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT e.id, e.action, e.target_type, e.target_id, e.metadata,
                      e.created_at, COALESCE(u.display_name, 'Deleted admin') AS actor
                 FROM platform_audit_event e
                 LEFT JOIN app_user u ON u.id = e.actor_user_id
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT $1 OFFSET $2""",
        limit,
        offset,
    )
    return [
        {
            **dict(row),
            "metadata": json.loads(row["metadata"])
            if isinstance(row["metadata"], str)
            else row["metadata"],
        }
        for row in rows
    ]


async def update_workspace_entitlement(
    connection: asyncpg.Connection,
    *,
    workspace_id: str,
    actor_user_id: str,
    plan: str | None = None,
    suspended: bool | None = None,
) -> dict[str, Any] | None:
    current = await connection.fetchrow(
        "SELECT plan, is_suspended, trial_expires_at FROM workspace WHERE id = $1 FOR UPDATE",
        workspace_id,
    )
    if not current:
        return None

    changes: dict[str, Any] = {}
    if plan is not None and plan != current["plan"]:
        changes["plan"] = {"from": current["plan"], "to": plan}
    if suspended is not None and suspended != current["is_suspended"]:
        changes["suspended"] = {"from": current["is_suspended"], "to": suspended}
    if not changes:
        return await get_workspace(connection, workspace_id)

    async with connection.transaction():
        if plan is not None and suspended is not None:
            await connection.execute(
                "UPDATE workspace SET plan = $2, is_suspended = $3, "
                "suspended_at = CASE WHEN $3 THEN NOW() ELSE NULL END, "
                "suspended_by = CASE WHEN $3 THEN $4 ELSE NULL END, "
                "trial_started_at = CASE WHEN $2 = 'TRIAL' THEN "
                "COALESCE(trial_started_at, NOW()) ELSE NULL END, "
                "trial_expires_at = CASE WHEN $2 = 'TRIAL' THEN "
                "COALESCE(trial_expires_at, NOW() + INTERVAL '7 days') ELSE NULL END "
                "WHERE id = $1",
                workspace_id, plan, suspended, actor_user_id,
            )
        elif plan is not None:
            await connection.execute(
                "UPDATE workspace SET plan = $2, "
                "trial_started_at = CASE WHEN $2 = 'TRIAL' THEN "
                "COALESCE(trial_started_at, NOW()) ELSE NULL END, "
                "trial_expires_at = CASE WHEN $2 = 'TRIAL' THEN "
                "COALESCE(trial_expires_at, NOW() + INTERVAL '7 days') ELSE NULL END "
                "WHERE id = $1",
                workspace_id, plan,
            )
        else:
            await connection.execute(
                "UPDATE workspace SET is_suspended = $2, "
                "suspended_at = CASE WHEN $2 THEN NOW() ELSE NULL END, "
                "suspended_by = CASE WHEN $2 THEN $3 ELSE NULL END "
                "WHERE id = $1",
                workspace_id, suspended, actor_user_id,
            )
        await record_audit(
            connection,
            actor_user_id=actor_user_id,
            action="UPDATE_WORKSPACE_ENTITLEMENT",
            target_type="workspace",
            target_id=workspace_id,
            metadata=changes,
        )
    return await get_workspace(connection, workspace_id)


async def workspace_metrics(
    connection: asyncpg.Connection, workspace_id: str
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """SELECT
               (SELECT COUNT(*) FROM comment_content c WHERE c.workspace_id = $1)
                   AS comments_processed,
               (SELECT COUNT(*) FROM comment_content c
                 WHERE c.workspace_id = $1
                   AND c.posted_at >= NOW() - INTERVAL '7 days') AS comments_processed_7d,
               (SELECT COUNT(*) FROM comment_content c
                 WHERE c.workspace_id = $1
                   AND NOT EXISTS (
                       SELECT 1 FROM action a WHERE a.comment_id = c.comment_id
                   )
                   AND EXISTS (
                       SELECT 1 FROM verdict v WHERE v.comment_id = c.comment_id
                         AND v.surfaced_reason <> 'cleared'
                   )) AS pending_comments,
               (SELECT COUNT(*) FROM comment_content c
                 WHERE c.workspace_id = $1
                   AND EXISTS (
                       SELECT 1 FROM action a WHERE a.comment_id = c.comment_id
                   )) AS reviewed_comments,
               (SELECT COUNT(*) FROM auto_reply_event e
                 WHERE e.workspace_id = $1
                   AND e.occurred_at >= NOW() - INTERVAL '7 days'
                   AND e.decision = 'replied' AND e.provider_applied = TRUE)
                   AS replies_sent_7d,
               (SELECT COUNT(*) FROM auto_reply_event e
                 WHERE e.workspace_id = $1
                   AND e.occurred_at >= NOW() - INTERVAL '7 days'
                   AND e.decision = 'skipped'
                   AND e.reason ILIKE 'Facebook reply failed:%') AS reply_failures_7d""",
        workspace_id,
    )
    return dict(row)
