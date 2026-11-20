"""Real worker processes and local HTTP receiver. Kill the owner after provider receipt, before its response."""

import asyncio
import os
import sys
from contextlib import suppress

from .test_worker import Settings, process_batch, seed


async def test_kill_mid_send_same_id(database):
    await seed(database)
    await process_batch(database, Settings.load())
    keys = []
    accepted = set()
    first = asyncio.Event()
    release = asyncio.Event()

    async def receive(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = {
                k.lower(): v.strip()
                for k, v in (line.split(":", 1) for line in head.split("\r\n")[1:] if ":" in line)
            }
            await reader.readexactly(int(headers["content-length"]))
            key = headers["idempotency-key"]
            keys.append(key)
            accepted.add(key)
            if len(keys) == 1:
                first.set()
                await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = {
        **os.environ,
        "DATABASE_URL": database.conninfo,
        "FAKE_TELECOM_URL": f"http://127.0.0.1:{port}",
    }

    async def spawn():
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alerting.delivery.worker", "--channels", "sms", env=env
        )

    children = []
    try:
        proc = await spawn()
        children.append(proc)
        await asyncio.wait_for(first.wait(), 10)
        proc.kill()
        assert await proc.wait() == -9
        release.set()
        # Let the replacement process wait out the production lease unmodified.
        children.append(await spawn())
        async with asyncio.timeout(42):
            while True:
                async with database.connection() as conn:
                    row = await (
                        await conn.execute(
                            "select id,state,attempts from notifications where channel='sms'"
                        )
                    ).fetchone()
                if row[1] == "delivered":
                    break
                await asyncio.sleep(0.05)
        assert row[2] == 2 and keys == [str(row[0])] * 2 and len(accepted) == 1
        async with database.connection() as conn:
            assert await (
                await conn.execute(
                    "select outcome from delivery_attempts where notification_id=%s order by attempt",
                    (row[0],),
                )
            ).fetchall() == [("unknown",), ("delivered",)]
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        for child in children:
            if child.returncode is None:
                child.terminate()
                await child.wait()
