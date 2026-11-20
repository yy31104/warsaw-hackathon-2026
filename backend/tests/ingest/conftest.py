import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import httpx
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from alerting import db
from alerting.api.app import app


if sys.platform == "win32":
    def pytest_asyncio_loop_factories(config, item):
        # psycopg's native async connections need a selector loop on Windows.
        return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture
async def inbox_pool():
    url = os.getenv("DATABASE_URL", db.DATABASE_URL)
    try:
        admin = await psycopg.AsyncConnection.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        if "DATABASE_URL" in os.environ:
            raise
        pytest.skip("PostgreSQL unavailable; set DATABASE_URL to run inbox integration tests")
    schema = "test_inbox_" + uuid4().hex
    migrations = Path(__file__).resolve().parents[3] / "db" / "migrations"
    async with admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            await admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
            for migration in sorted(migrations.glob("*.sql")):
                await admin.execute(migration.read_text(encoding="utf-8"))
            async with AsyncConnectionPool(url, min_size=1, max_size=10, open=False, kwargs={
                "options": f"-c search_path={schema},public",
            }) as pool:
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
async def client(inbox_pool, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, db.pool, lambda: inbox_pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://inbox.test") as client:
        yield client
