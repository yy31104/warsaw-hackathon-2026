"""Database pool. SHARED FILE.

Usage:
    async with pool().connection() as conn:
        async with conn.transaction():
            await conn.execute(...)
"""
from __future__ import annotations

import os

from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://alerting:alerting@localhost:5432/alerting")

_pool: AsyncConnectionPool | None = None


def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            DATABASE_URL,
            min_size=int(os.getenv("DB_POOL_MIN", "1")),
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            open=False,
        )
    return _pool


async def open_pool() -> None:
    await pool().open()


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()
