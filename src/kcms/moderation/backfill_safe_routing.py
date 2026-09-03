"""One-off data fix for comments stuck on `uncertainty` despite already
being SAFE.

`pattern_matcher._route()` used to check `abstain` before severity, so a
SAFE-severity comment with an ambiguous target (abstain=True) surfaced as
"uncertain" instead of clearing. That's fixed for anything classified from
now on, but Verdict is append-only — nothing recomputes an already-stored
comment automatically. This finds every comment whose *latest* verdict is
SAFE but not `cleared`, and appends a corrected verdict for it (same
severity/target/confidence/rationale the model actually produced — only
the routing outcome changes), rather than rewriting history.

    python -m kcms.moderation.backfill_safe_routing            # apply
    python -m kcms.moderation.backfill_safe_routing --dry-run  # preview only
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from kcms.settings import settings
from kcms.shared.database import database

# Comment text is Khmer; a Windows console's default codepage (cp1252) can't
# encode it and crashes mid-run, right before the fix would have applied.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_LATEST_VERDICT_PER_COMMENT = """
    -- The latest verdict per comment must be picked BEFORE filtering on its
    -- surfaced_reason. Filtering first (as an earlier version of this query
    -- did) can pick a stale row: once a comment has a 'cleared' verdict on
    -- top of an older 'uncertainty' one, filtering out 'cleared' rows before
    -- DISTINCT ON leaves the stale row looking like the latest again.
    WITH latest AS (
        SELECT DISTINCT ON (comment_id)
            comment_id, severity, severity_confidence, target, target_confidence,
            abstain, surfaced_reason, rationale, model_version
        FROM verdict
        ORDER BY comment_id, occurred_at DESC, id DESC
    )
    SELECT comment_id, severity, severity_confidence, target, target_confidence,
           abstain, rationale, model_version
    FROM latest
    WHERE severity = 'SAFE' AND surfaced_reason <> 'cleared'
"""


async def run(dry_run: bool) -> None:
    await database.connect(settings.database_url)
    try:
        async with database.acquire() as connection:
            stuck = await connection.fetch(_LATEST_VERDICT_PER_COMMENT)

            if not stuck:
                print("Nothing to fix - no SAFE verdict is stuck off 'cleared'.")
                return

            print(f"{len(stuck)} comment(s) stuck on a non-cleared SAFE verdict:")
            for row in stuck:
                text = await connection.fetchval(
                    "SELECT text FROM comment_content WHERE comment_id = $1", row["comment_id"]
                )
                preview = (text or "")[:60]
                print(f"  {row['comment_id']}: {preview!r}")

            if dry_run:
                print("\nDry run — nothing written. Re-run without --dry-run to apply.")
                return

            async with connection.transaction():
                for row in stuck:
                    await connection.execute(
                        """INSERT INTO verdict
                           (comment_id, severity, severity_confidence, target, target_confidence,
                            abstain, surfaced_reason, rationale, model_version)
                           VALUES ($1, $2, $3, $4, $5, $6, 'cleared', $7, $8)""",
                        row["comment_id"], row["severity"], row["severity_confidence"],
                        row["target"], row["target_confidence"], row["abstain"],
                        row["rationale"], row["model_version"],
                    )
            print(f"\nAppended a corrected 'cleared' verdict for {len(stuck)} comment(s).")
    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="list affected comments without writing"
    )
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))
