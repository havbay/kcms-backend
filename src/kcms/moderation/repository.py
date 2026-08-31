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
    a.occurred_at AS latest_action_at,
    k.severity    AS corrected_severity,
    k.target      AS corrected_target,
    k.actor       AS corrected_by,
    k.occurred_at AS corrected_at
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
LEFT JOIN LATERAL (
    SELECT * FROM correction k2
    WHERE k2.comment_id = c.comment_id
    ORDER BY k2.occurred_at DESC, k2.id DESC LIMIT 1
) k ON TRUE
WHERE c.workspace_id = $1
ORDER BY
    -- unresolved first, then most severe, then oldest
    (a.kind IS NOT NULL),
    CASE v.severity WHEN 'HARMFUL' THEN 0 WHEN 'OFFENSIVE' THEN 1 ELSE 2 END,
    c.posted_at
"""


async def seed_workspace(connection: asyncpg.Connection, workspace_id: str) -> int:
    """Give a new workspace its own copy of the sample comments.

    Comment ids are prefixed with the workspace so they stay globally unique
    without a composite key on every downstream table.
    """
    existing = await connection.fetchval(
        "SELECT COUNT(*) FROM comment_content WHERE workspace_id = $1", workspace_id
    )
    if existing:
        return 0

    prefix = workspace_id[:8]
    contexts = [
        CommentContext(comment_id=f"{prefix}-{s.comment_id}", text=s.text) for s in SEED_COMMENTS
    ]
    verdicts = await PatternMatcher().classify(contexts)

    async with connection.transaction():
        for seed, context, verdict in zip(SEED_COMMENTS, contexts, verdicts, strict=True):
            await connection.execute(
                """INSERT INTO comment_content
                   (comment_id, page_id, workspace_id, author_ref, text,
                    post_text, parent_text, is_reply)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                context.comment_id, PAGE_ID, workspace_id, seed.author_ref, seed.text,
                seed.post_text, seed.parent_text, seed.is_reply,
            )
            await connection.execute(
                """INSERT INTO verdict
                   (comment_id, severity, severity_confidence, target, target_confidence,
                    abstain, surfaced_reason, rationale, model_version)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                context.comment_id, verdict.severity.value, verdict.severity_confidence,
                verdict.target.value, verdict.target_confidence, verdict.abstain,
                verdict.surfaced_reason.value, verdict.rationale, verdict.model_version,
            )
    return len(SEED_COMMENTS)


async def comment_belongs_to(
    connection: asyncpg.Connection, comment_id: str, workspace_id: str
) -> bool:
    """Authorization, not validation: without this a caller could act on another
    workspace's comment by guessing its id."""
    return bool(
        await connection.fetchval(
            "SELECT 1 FROM comment_content WHERE comment_id = $1 AND workspace_id = $2",
            comment_id, workspace_id,
        )
    )


async def fetch_work_list(
    connection: asyncpg.Connection, workspace_id: str
) -> list[dict[str, Any]]:
    rows = await connection.fetch(WORK_LIST_SQL, workspace_id)
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


async def record_correction(
    connection: asyncpg.Connection,
    comment_id: str,
    severity: str,
    target: str,
    actor: str,
    note: str | None,
) -> dict[str, Any] | None:
    """Append a Correction: what a human says the labels should be.

    Deliberately writes NO Action. Disagreeing with a label is not a decision
    about the comment, and conflating the two is how a moderation tool starts
    manufacturing training labels nobody gave.
    """
    model_version = await connection.fetchval(
        """SELECT model_version FROM verdict
           WHERE comment_id = $1 ORDER BY occurred_at DESC, id DESC LIMIT 1""",
        comment_id,
    )
    row = await connection.fetchrow(
        """INSERT INTO correction
           (comment_id, severity, target, disagrees_with_model, note, actor)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING severity, target, actor, note, disagrees_with_model, occurred_at""",
        comment_id, severity, target, model_version or "unknown", note, actor,
    )
    return dict(row) if row else None
