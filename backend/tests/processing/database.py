"""Isolated PostgreSQL schema per test; never reuse the application tables."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest_asyncio
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool


@pytest_asyncio.fixture
async def database():
    dsn = os.getenv("DATABASE_URL", "postgresql://alerting:alerting@localhost:5432/alerting")
    try:
        admin = await psycopg.AsyncConnection.connect(dsn, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    schema = "test_" + uuid4().hex
    async with admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            scoped = make_conninfo(dsn, options=f"-c search_path={schema},public")
            async with AsyncConnectionPool(scoped, min_size=1, max_size=6, open=False) as pool:
                async with pool.connection() as conn:
                    migrations = Path(__file__).resolve().parents[3] / "db/migrations"
                    for path in sorted(migrations.glob("*.sql")):
                        await conn.execute(path.read_text())
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
