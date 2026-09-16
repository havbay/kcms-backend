"""Owns the asyncpg pool. Application services own transaction boundaries."""

from __future__ import annotations

import asyncio
from types import TracebackType

import asyncpg


class _PerRequestConnection:
    """Open one connection, hand it out, and close it again.

    Cloudflare Workers forbid reusing an I/O object across requests. A pooled
    socket is owned by the request that opened it, so a later request on the
    same isolate blocks on it forever and the runtime cancels the request with
    "your Worker's code had hung". Hyperdrive does the pooling on Cloudflare's
    side, which is what makes connecting per request affordable there.
    """

    def __init__(self, dsn: str, timeout_seconds: float) -> None:
        self._dsn = dsn
        self._timeout_seconds = timeout_seconds
        self._connection: asyncpg.Connection | None = None

    async def __aenter__(self) -> asyncpg.Connection:
        self._connection = await asyncio.wait_for(
            asyncpg.connect(self._dsn), timeout=self._timeout_seconds
        )
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._leases = 0
        self._dsn: str | None = None
        self._timeout_seconds: float = 5
        self._per_request = False

    async def connect(
        self,
        dsn: str,
        *,
        timeout_seconds: float = 5,
        per_request: bool = False,
    ) -> None:
        self._dsn = dsn
        self._timeout_seconds = timeout_seconds
        if per_request:
            # No connection is opened here on purpose: one opened during
            # startup would belong to whichever request triggered startup.
            self._per_request = True
            self._leases += 1
            return
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

    def set_dsn(self, dsn: str) -> None:
        """Point later per-request connections at ``dsn``.

        The Worker reads its Hyperdrive binding on every request, so the
        address can differ from the one seen at startup.
        """
        self._dsn = dsn

    async def disconnect(self) -> None:
        if self._per_request:
            if self._leases > 0:
                self._leases -= 1
            if self._leases == 0:
                self._per_request = False
                self._dsn = None
            return
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
        """Acquire a connection. Raises if the database is not connected."""
        if self._per_request:
            if self._dsn is None:
                raise RuntimeError("database pool is not connected")
            return _PerRequestConnection(self._dsn, self._timeout_seconds)
        if self._pool is None:
            raise RuntimeError("database pool is not connected")
        return self._pool.acquire()

    @property
    def connected(self) -> bool:
        if self._per_request:
            return self._dsn is not None
        return self._pool is not None

    async def is_reachable(self) -> bool:
        """Probe PostgreSQL. Never leaks connection or exception detail."""
        if not self.connected:
            return False
        try:
            async with self.acquire() as connection:
                return await connection.fetchval("SELECT 1") == 1
        except Exception:
            return False


database = Database()
