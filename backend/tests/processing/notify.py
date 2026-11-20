"""Capture committed notifications from connections in a test's isolated application."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
from psycopg import sql


@asynccontextmanager
async def listen(db):
    fence = "test_done_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(db.conninfo, autocommit=True) as conn:
        for channel in ("alerts", "actions", "notifications", fence):
            await conn.execute(sql.SQL("listen {}").format(sql.Identifier(channel)))

        async def drain():
            pids = {
                r[0]
                for r in await (
                    await conn.execute(
                        "select pid from pg_stat_activity where application_name=current_setting('application_name')"
                    )
                ).fetchall()
            }
            await conn.execute("select pg_notify(%s,'')", (fence,))
            signals = []
            async with asyncio.timeout(5):
                async for signal in conn.notifies():
                    if signal.channel == fence:
                        return signals
                    if signal.pid in pids:
                        signals.append((signal.channel, json.loads(signal.payload)))

        yield drain
