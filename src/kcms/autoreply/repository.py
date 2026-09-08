"""Persistence adapter for workspace automated-reply configuration and logs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg

from kcms.autoreply.rules import ReplyRule


def _rule(row: asyncpg.Record) -> ReplyRule:
    return ReplyRule(
        id=row["id"],
        name=row["name"],
        keywords=tuple(row["keywords"]),
        reply_body=row["reply_body"],
        on_comments=row["on_comments"],
        on_messages=row["on_messages"],
        position=row["position"],
        enabled=row["enabled"],
    )


async def list_rules(connection: asyncpg.Connection, workspace_id: str) -> list[ReplyRule]:
    rows = await connection.fetch(
        """SELECT id, name, keywords, reply_body, on_comments, on_messages, position, enabled
           FROM auto_reply_rule
           WHERE workspace_id = $1
           ORDER BY position, id""",
        workspace_id,
    )
    return [_rule(row) for row in rows]


async def create_rule(
    connection: asyncpg.Connection,
    workspace_id: str,
    rule: ReplyRule,
    created_by: str,
) -> ReplyRule:
    row = await connection.fetchrow(
        """INSERT INTO auto_reply_rule
           (id, workspace_id, name, keywords, reply_body, on_comments, on_messages,
            position, enabled, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING id, name, keywords, reply_body, on_comments, on_messages, position, enabled""",
        rule.id, workspace_id, rule.name, list(rule.keywords), rule.reply_body,
        rule.on_comments, rule.on_messages, rule.position, rule.enabled, created_by,
    )
    return _rule(row)


async def get_rule(
    connection: asyncpg.Connection, workspace_id: str, rule_id: str
) -> ReplyRule | None:
    row = await connection.fetchrow(
        """SELECT id, name, keywords, reply_body, on_comments, on_messages, position, enabled
           FROM auto_reply_rule WHERE workspace_id = $1 AND id = $2""",
        workspace_id, rule_id,
    )
    return _rule(row) if row else None


async def update_rule(
    connection: asyncpg.Connection,
    workspace_id: str,
    rule: ReplyRule,
) -> ReplyRule | None:
    row = await connection.fetchrow(
        """UPDATE auto_reply_rule
           SET name = $3, keywords = $4, reply_body = $5, on_comments = $6,
               on_messages = $7, position = $8, enabled = $9, updated_at = NOW()
           WHERE workspace_id = $1 AND id = $2
           RETURNING id, name, keywords, reply_body, on_comments, on_messages, position, enabled""",
        workspace_id, rule.id, rule.name, list(rule.keywords), rule.reply_body,
        rule.on_comments, rule.on_messages, rule.position, rule.enabled,
    )
    return _rule(row) if row else None


async def delete_rule(connection: asyncpg.Connection, workspace_id: str, rule_id: str) -> bool:
    result = await connection.execute(
        "DELETE FROM auto_reply_rule WHERE workspace_id = $1 AND id = $2",
        workspace_id, rule_id,
    )
    return result.endswith("1")


async def reorder_rules(
    connection: asyncpg.Connection, workspace_id: str, rule_ids: Sequence[str]
) -> bool:
    existing = await connection.fetch(
        "SELECT id FROM auto_reply_rule WHERE workspace_id = $1 ORDER BY position, id",
        workspace_id,
    )
    if {row["id"] for row in existing} != set(rule_ids) or len(rule_ids) != len(existing):
        return False
    async with connection.transaction():
        for position, rule_id in enumerate(rule_ids):
            await connection.execute(
                "UPDATE auto_reply_rule SET position = $3, updated_at = NOW() "
                "WHERE workspace_id = $1 AND id = $2",
                workspace_id, rule_id, position,
            )
    return True


async def set_settings(
    connection: asyncpg.Connection,
    workspace_id: str,
    *,
    enabled: bool | None = None,
) -> None:
    await connection.execute(
        """UPDATE workspace
           SET auto_reply_enabled = COALESCE($2, auto_reply_enabled)
           WHERE id = $1""",
        workspace_id, enabled,
    )


async def list_events(
    connection: asyncpg.Connection, workspace_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT id, rule_id, provider_event_id, channel, decision, reason,
                  reply_body, provider_applied, occurred_at
           FROM auto_reply_event
           WHERE workspace_id = $1
           ORDER BY occurred_at DESC, id DESC
           LIMIT $2""",
        workspace_id, limit,
    )
    return [dict(row) for row in rows]


async def event_exists(
    connection: asyncpg.Connection,
    workspace_id: str,
    channel: str,
    provider_event_id: str,
) -> bool:
    return bool(await connection.fetchval(
        """SELECT 1 FROM auto_reply_event
           WHERE workspace_id = $1 AND channel = $2 AND provider_event_id = $3""",
        workspace_id, channel, provider_event_id,
    ))


async def record_event(
    connection: asyncpg.Connection,
    workspace_id: str,
    *,
    rule_id: str | None,
    provider_event_id: str,
    channel: str,
    decision: str,
    reason: str,
    reply_body: str | None,
    provider_applied: bool = False,
) -> bool:
    """Record one provider decision without allowing duplicate replies."""
    row = await connection.fetchrow(
        """INSERT INTO auto_reply_event
           (workspace_id, rule_id, provider_event_id, channel, decision, reason,
            reply_body, provider_applied)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (workspace_id, channel, provider_event_id) DO NOTHING
           RETURNING id""",
        workspace_id, rule_id, provider_event_id, channel, decision, reason,
        reply_body, provider_applied,
    )
    return row is not None


async def mark_event_replied(
    connection: asyncpg.Connection,
    workspace_id: str,
    channel: str,
    provider_event_id: str,
) -> None:
    await connection.execute(
        """UPDATE auto_reply_event
           SET decision = 'replied', reason = 'reply posted to Facebook',
               provider_applied = TRUE
           WHERE workspace_id = $1 AND channel = $2 AND provider_event_id = $3""",
        workspace_id, channel, provider_event_id,
    )


async def mark_event_failed(
    connection: asyncpg.Connection,
    workspace_id: str,
    channel: str,
    provider_event_id: str,
    reason: str,
) -> None:
    await connection.execute(
        """UPDATE auto_reply_event
           SET decision = 'skipped', reason = $4, provider_applied = FALSE
           WHERE workspace_id = $1 AND channel = $2 AND provider_event_id = $3""",
        workspace_id, channel, provider_event_id, reason,
    )
