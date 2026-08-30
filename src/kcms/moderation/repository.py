"""Reads and writes the moderation spine. Owns its transaction boundaries."""

from __future__ import annotations

from typing import Any

import asyncpg

from kcms.moderation.contracts import CommentContext
from kcms.moderation.pattern_matcher import PatternMatcher
from kcms.moderation.seeds import PAGE_ID, SEED_COMMENTS

WORK_LIST_SQL = """
SELECT
    c.comment_id,
    c.text,
    c.author_ref,
    c.posted_at,
    v.severity,
    v.severity_confidence,
    v.target,
    v.target_confidence,
    v.abstain,
    v.surfaced_reason,
    v.rationale,
    v.model_version,
    a.kind        AS latest_action,
    a.actor       AS latest_actor,
    a.occurred_at AS latest_action_at
FROM comment_content c
LEFT JOIN LATERAL (
    SELECT * FROM verdict v2
    WHERE v2.comment_id = c.comment_id
    ORDER BY v2.occurred_at DESC, v2.id DESC LIMIT 1
) v ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM action a2
    WHERE a2.comment_id = c.comment_id
    ORDER BY a2.occurred_at DESC, a2.id DESC LIMIT 1
) a ON TRUE
WHERE c.page_id = $1
ORDER BY
    -- unresolved first, then most severe, then oldest
    (a.kind IS NOT NULL),
    CASE v.severity WHEN 'HARMFUL' THEN 0 WHEN 'OFFENSIVE' THEN 1 ELSE 2 END,
    c.posted_at
"""


async def seed_if_empty(connection: asyncpg.Connection) -> int:
    """Classify and store the scripted comments once. Idempotent."""
    existing = await connection.fetchval(
        "SELECT COUNT(*) FROM comment_content WHERE page_id = $1", PAGE_ID
    )
    if existing:
        return 0

    verdicts = await PatternMatcher().classify(
        [CommentContext(comment_id=s.comment_id, text=s.text) for s in SEED_COMMENTS]
    )

    async with connection.transaction():
        for seed, verdict in zip(SEED_COMMENTS, verdicts, strict=True):
            await connection.execute(
                """INSERT INTO comment_content
                   (comment_id, page_id, author_ref, text, post_text, parent_text, is_reply)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                seed.comment_id, PAGE_ID, seed.author_ref, seed.text,
                seed.post_text, seed.parent_text, seed.is_reply,
            )
            await connection.execute(
                """INSERT INTO verdict
                   (comment_id, severity, severity_confidence, target, target_confidence,
                    abstain, surfaced_reason, rationale, model_version)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                seed.comment_id, verdict.severity.value, verdict.severity_confidence,
                verdict.target.value, verdict.target_confidence, verdict.abstain,
                verdict.surfaced_reason.value, verdict.rationale, verdict.model_version,
            )
    return len(SEED_COMMENTS)


async def fetch_work_list(connection: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await connection.fetch(WORK_LIST_SQL, PAGE_ID)
    return [dict(row) for row in rows]


async def record_action(
    connection: asyncpg.Connection, comment_id: str, kind: str, actor: str
) -> None:
    """Append an Action. Writes NO Correction: hiding a comment is not a
    statement that the model's label was wrong (ARCHITECTURE section 6)."""
    await connection.execute(
        "INSERT INTO action (comment_id, kind, actor) VALUES ($1, $2, $3)",
        comment_id, kind, actor,
    )


async def fetch_history(connection: asyncpg.Connection, comment_id: str) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT kind, actor, occurred_at FROM action
           WHERE comment_id = $1 ORDER BY occurred_at DESC, id DESC""",
        comment_id,
    )
    return [dict(row) for row in rows]
