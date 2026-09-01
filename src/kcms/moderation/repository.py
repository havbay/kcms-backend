"""Reads and writes the moderation spine. Owns its transaction boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg

from kcms.integrations.contracts import ProviderComment
from kcms.moderation.contracts import CommentContext
from kcms.moderation.pattern_matcher import PatternMatcher
from kcms.moderation.seeds import PAGE_ID, SEED_COMMENTS

# Once a workspace connects a Page, the queue is that Page's comments. The
# seeded samples exist so the screens are not empty before a connection, and
# showing them alongside real comments makes the queue untrustworthy. Without a
# connection nothing is filtered, so a new workspace still has something to show.
CONNECTED_PAGE_ONLY = """
    AND (
        NOT EXISTS (SELECT 1 FROM page_connection pc WHERE pc.workspace_id = $1)
        OR c.page_id = (SELECT external_page_id FROM page_connection WHERE workspace_id = $1)
    )
"""

WORK_LIST_SELECT = """
SELECT
    c.comment_id,
    c.text,
    c.author_ref,
    c.posted_at,
    c.page_id,
    c.post_text,
    c.parent_text,
    c.is_reply,
    c.post_kind,
    c.post_permalink,
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
"""

WORK_LIST_FROM = """
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
                    post_text, parent_text, is_reply, post_kind, post_permalink)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                context.comment_id, PAGE_ID, workspace_id, seed.author_ref, seed.text,
                seed.post_text, seed.parent_text, seed.is_reply,
                seed.post_kind, seed.post_permalink,
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
    connection: asyncpg.Connection,
    workspace_id: str,
    limit: int = 25,
    offset: int = 0,
    query: str | None = None,
    severity: str | None = None,
    target: str | None = None,
    surfaced_reason: str | None = None,
    review_status: str | None = None,
    sort: str = "PRIORITY",
) -> tuple[list[dict[str, Any]], int]:
    """Returns (page, total). A Page under real load produces thousands of
    comments; returning them all was only ever viable with seeded data."""
    values: list[Any] = [workspace_id]
    conditions = ["c.workspace_id = $1"]

    def add(value: Any, sql: str) -> None:
        values.append(value)
        conditions.append(sql.replace("$?", f"${len(values)}"))

    if query:
        add(query.strip(), "(c.text ILIKE '%' || $? || '%' OR c.post_text ILIKE '%' || $? || '%')")
    if severity:
        add(severity, "v.severity = $?")
    if target:
        add(target, "v.target = $?")
    if surfaced_reason:
        add(surfaced_reason, "v.surfaced_reason = $?")
    if review_status == "PENDING":
        conditions.append("a.kind IS NULL")
    elif review_status == "ACTIONED":
        conditions.append("a.kind IS NOT NULL")

    where = " WHERE " + " AND ".join(conditions) + CONNECTED_PAGE_ONLY
    order = {
        "NEWEST": "c.posted_at DESC, c.comment_id",
        "OLDEST": "c.posted_at, c.comment_id",
        "PRIORITY": """(a.kind IS NOT NULL),
            CASE v.severity WHEN 'HARMFUL' THEN 0 WHEN 'OFFENSIVE' THEN 1 ELSE 2 END,
            c.posted_at, c.comment_id""",
    }[sort]
    count_sql = "SELECT COUNT(*) " + WORK_LIST_FROM + where
    total = await connection.fetchval(count_sql, *values)
    values.extend([limit, offset])
    page_sql = (
        WORK_LIST_SELECT + WORK_LIST_FROM + where + f" ORDER BY {order} "
        f"LIMIT ${len(values) - 1} OFFSET ${len(values)}"
    )
    rows = await connection.fetch(page_sql, *values)
    return [dict(row) for row in rows], total


async def summarise_workspace(
    connection: asyncpg.Connection, workspace_id: str
) -> dict[str, Any]:
    """Counts computed in the database rather than from a page of results.

    The Overview previously derived its figures from whatever the work list
    happened to return, which silently becomes wrong the moment that list is
    paginated.
    """
    row = await connection.fetchrow(
        """
        WITH latest_verdict AS (
            SELECT DISTINCT ON (comment_id) comment_id, surfaced_reason
            FROM verdict ORDER BY comment_id, occurred_at DESC, id DESC
        ),
        latest_action AS (
            SELECT DISTINCT ON (comment_id) comment_id, kind
            FROM action ORDER BY comment_id, occurred_at DESC, id DESC
        )
        SELECT
            COUNT(*) AS processed,
            COUNT(*) FILTER (
                WHERE v.surfaced_reason IS NOT NULL AND v.surfaced_reason <> 'cleared'
            ) AS need_review,
            COUNT(*) FILTER (WHERE a.kind IS NOT NULL) AS reviewed,
            COUNT(*) FILTER (
                WHERE v.surfaced_reason IS NOT NULL AND v.surfaced_reason <> 'cleared'
                  AND a.kind IS NULL
            ) AS pending,
            COUNT(*) FILTER (WHERE a.kind = 'LEAVE') AS left_visible,
            COUNT(*) FILTER (WHERE a.kind = 'HIDE') AS hidden,
            COUNT(*) FILTER (WHERE a.kind = 'UNHIDE') AS unhidden
        FROM comment_content c
        LEFT JOIN latest_verdict v ON v.comment_id = c.comment_id
        LEFT JOIN latest_action a ON a.comment_id = c.comment_id
        WHERE c.workspace_id = $1
        """
        + CONNECTED_PAGE_ONLY,
        workspace_id,
    )
    reasons = await connection.fetch(
        """
        WITH latest_verdict AS (
            SELECT DISTINCT ON (v.comment_id) v.comment_id, v.surfaced_reason
            FROM verdict v
            JOIN comment_content c ON c.comment_id = v.comment_id
            WHERE c.workspace_id = $1
            ORDER BY v.comment_id, v.occurred_at DESC, v.id DESC
        )
        SELECT surfaced_reason, COUNT(*) AS count
        FROM latest_verdict
        WHERE surfaced_reason <> 'cleared'
        GROUP BY surfaced_reason ORDER BY count DESC
        """,
        workspace_id,
    )
    summary = dict(row)
    summary["reasons"] = [dict(r) for r in reasons]
    return summary


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


async def ingest_provider_comments(
    connection: asyncpg.Connection,
    workspace_id: str,
    page_id: str,
    comments: Sequence[ProviderComment],
) -> int:
    """Store comments pulled from the provider and classify the new ones.

    The provider's own comment id is the primary key, so re-syncing the same
    Page is idempotent: an existing comment is skipped rather than duplicated,
    and its verdict, actions and corrections survive the next sync.
    """
    if not comments:
        return 0

    existing = {
        row["comment_id"]
        for row in await connection.fetch(
            "SELECT comment_id FROM comment_content WHERE comment_id = ANY($1::TEXT[])",
            [c.comment_id for c in comments],
        )
    }
    fresh = [c for c in comments if c.comment_id not in existing]
    if not fresh:
        return 0

    contexts = [
        CommentContext(
            comment_id=c.comment_id,
            text=c.text,
            is_reply=c.is_reply,
            parent_text=c.parent_text,
            post_text=c.post_text,
        )
        for c in fresh
    ]
    verdicts = await PatternMatcher().classify(contexts)

    async with connection.transaction():
        for comment, verdict in zip(fresh, verdicts, strict=True):
            await connection.execute(
                """INSERT INTO comment_content
                   (comment_id, page_id, workspace_id, author_ref, text,
                    post_text, parent_text, is_reply, post_kind, post_permalink, posted_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                   ON CONFLICT (comment_id) DO NOTHING""",
                comment.comment_id, page_id, workspace_id, comment.author_ref, comment.text,
                comment.post_text, comment.parent_text, comment.is_reply,
                comment.post_kind, comment.post_permalink, comment.created_time,
            )
            await connection.execute(
                """INSERT INTO verdict
                   (comment_id, severity, severity_confidence, target, target_confidence,
                    abstain, surfaced_reason, rationale, model_version)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                comment.comment_id, verdict.severity.value, verdict.severity_confidence,
                verdict.target.value, verdict.target_confidence, verdict.abstain,
                verdict.surfaced_reason.value, verdict.rationale, verdict.model_version,
            )
    return len(fresh)


async def comment_page_id(
    connection: asyncpg.Connection, comment_id: str
) -> str | None:
    """The Page a stored comment came from.

    Seeded sample comments carry the sandbox Page id, so this is what
    separates a comment that must be mirrored to Facebook from one that
    exists only in KCMS.
    """
    return await connection.fetchval(
        "SELECT page_id FROM comment_content WHERE comment_id = $1", comment_id
    )


async def delete_sample_comments(connection: asyncpg.Connection, workspace_id: str) -> int:
    """Remove this workspace's seeded sample comments and everything about them.

    Scoped by the sample Page id as well as the workspace, so a comment
    imported from a connected Facebook Page can never be caught by it.

    Verdicts, actions and corrections are append-only but they are records
    *about* a comment. Once the comment is gone they describe nothing, so they
    are removed with it rather than left as orphans.
    """
    ids = [
        row["comment_id"]
        for row in await connection.fetch(
            "SELECT comment_id FROM comment_content WHERE workspace_id = $1 AND page_id = $2",
            workspace_id,
            PAGE_ID,
        )
    ]
    if not ids:
        return 0

    async with connection.transaction():
        for table in ("action", "verdict", "correction"):
            await connection.execute(
                f"DELETE FROM {table} WHERE comment_id = ANY($1::TEXT[])", ids
            )
        await connection.execute(
            "DELETE FROM comment_content WHERE comment_id = ANY($1::TEXT[])", ids
        )
        # The sample-data notice is about these comments. With them gone it
        # would be describing something that is no longer there.
        await connection.execute(
            "UPDATE workspace SET is_sandbox = FALSE WHERE id = $1", workspace_id
        )
    return len(ids)
