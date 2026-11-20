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


async def configure_connection(conn):
    # The 500-event hot-key test took 120ms over five batches on local PostgreSQL.
    for setting, key, default_ms in (
        ("lock_timeout", "DB_LOCK_TIMEOUT_MS", 1000),
        ("statement_timeout", "DB_STATEMENT_TIMEOUT_MS", 5000),
        ("idle_in_transaction_session_timeout", "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 15000),
    ):
        ms = int(os.getenv(key, default_ms))
        if ms <= 0:
            raise ValueError(f"{key} must be positive")
        await conn.execute("select set_config(%s,%s,false)", (setting, str(ms)))
    await conn.commit()


def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            DATABASE_URL,
            min_size=int(os.getenv("DB_POOL_MIN", "1")),
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            configure=configure_connection,
            open=False,
        )
    return _pool


async def open_pool() -> None:
    await pool().open()


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()
