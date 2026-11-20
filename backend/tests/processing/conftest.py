"""Isolated PostgreSQL schema per test; never reuse the application tables."""

import os
from pathlib import Path
from uuid import uuid4

import httpx
from alerting.api.app import app
from alerting.rules.storage import close_settings, get_db
import psycopg
import pytest
import pytest_asyncio
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
            scoped = make_conninfo(
                dsn, options=f"-c search_path={schema},public", application_name=schema
            )
            async with AsyncConnectionPool(scoped, min_size=1, max_size=6, open=False) as pool:
                async with pool.connection() as conn:
                    migrations = Path(__file__).resolve().parents[3] / "db/migrations"
                    for path in sorted(migrations.glob("*.sql")):
                        await conn.execute(path.read_text())
                try:
                    yield pool
                finally:
                    await close_settings(pool)
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest_asyncio.fixture
async def api(database):
    app.dependency_overrides[get_db] = lambda: database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
