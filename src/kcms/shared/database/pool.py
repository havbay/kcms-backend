"""Owns the asyncpg pool. Application services own transaction boundaries."""

from __future__ import annotations

import asyncpg


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self, dsn: str) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def is_reachable(self) -> bool:
        """Probe PostgreSQL. Never leaks connection or exception detail."""
        if self._pool is None:
            return False
        try:
            async with self._pool.acquire() as connection:
                return await connection.fetchval("SELECT 1") == 1
        except Exception:
            return False


database = Database()
