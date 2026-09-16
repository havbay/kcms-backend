"""Owns the asyncpg pool. Application services own transaction boundaries."""

from __future__ import annotations

import asyncio

import asyncpg


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._leases = 0

    async def connect(self, dsn: str, *, timeout_seconds: float = 5) -> None:
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncio.wait_for(
                    asyncpg.create_pool(
                        dsn,
                        min_size=1,
                        max_size=5,
                        timeout=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            self._leases += 1

    async def disconnect(self) -> None:
        pool: asyncpg.Pool | None = None
        async with self._lock:
            if self._leases > 0:
                self._leases -= 1
            if self._leases == 0 and self._pool is not None:
                pool = self._pool
                self._pool = None
        if pool is not None:
            await pool.close()

    def acquire(self):
        """Acquire a connection. Raises if the pool is not connected."""
        if self._pool is None:
            raise RuntimeError("database pool is not connected")
        return self._pool.acquire()

    @property
    def connected(self) -> bool:
        return self._pool is not None

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
