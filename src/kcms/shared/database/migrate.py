"""Forward-only SQL migrations, applied in filename order."""

from __future__ import annotations

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3].parent / "migrations"

_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def apply_migrations(connection: asyncpg.Connection) -> list[str]:
    await connection.execute(_TRACKING)
    rows = await connection.fetch("SELECT filename FROM schema_migration")
    applied = {r["filename"] for r in rows}
    newly: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        async with connection.transaction():
            await connection.execute(path.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO schema_migration (filename) VALUES ($1)", path.name
            )
        newly.append(path.name)
    return newly
