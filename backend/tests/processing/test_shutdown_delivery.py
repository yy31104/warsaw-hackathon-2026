"""Signal the real delivery CLI after receipt and reconcile the outbox after restart."""

import asyncio
import os
import sys
from contextlib import suppress

import pytest

from .test_worker import Settings, process_batch, seed


@pytest.mark.parametrize("outcome", ["delivered", "unknown"])
async def test_sigterm_drains_inflight_and_leaves_backlog_for_restart(database, outcome):
    count = 8
    settings = Settings.load()
    settings.ruleset.rules[0][0]["group"]["by"] = ["workflowId"]
    await seed(database, count)
    assert await process_batch(database, settings) == count

    first_received = asyncio.Event()
    reply = asyncio.Event()
    received_ids = []

    async def receive(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")[1:]
            headers = {k.lower(): v.strip() for k, v in (h.split(":", 1) for h in headers if h)}
            await reader.readexactly(int(headers["content-length"]))
            received_ids.append(headers["idempotency-key"])
            if len(received_ids) == 1:
                first_received.set()
                await reply.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except ConnectionError:
            pass  # The first receiver may have accepted the send but lost its response.
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    env = {
        **os.environ,
        "DATABASE_URL": database.conninfo,
        "FAKE_TELECOM_URL": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
    }

    async def spawn():
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alerting.delivery.worker", "--channels", "sms", env=env
        )

    async def rows():
        async with database.connection() as conn:
            return await (await conn.execute(
                """select id::text,state,attempts,locked_by,locked_until
                from notifications where channel='sms'"""
            )).fetchall()

    children = []
    async with server:
        try:
            child = await spawn()
            children.append(child)
            await asyncio.wait_for(first_received.wait(), 12)
            child.terminate()
            if outcome == "delivered":
                reply.set()
            assert await asyncio.wait_for(child.wait(), 8) == 0
            reply.set()

            after_signal = await rows()
            assert len(after_signal) == count
            assert len(received_ids) == 1
            first_id = received_ids[0]
            assert (first_id, outcome, 1, None, None) in after_signal
            assert all(row[1:] == ("pending", 0, None, None) for row in after_signal if row[0] != first_id)

            replacement = await spawn()
            children.append(replacement)
            async with asyncio.timeout(12):
                while any(row[1] != "delivered" for row in await rows()):
                    await asyncio.sleep(0.05)
            replacement.terminate()
            assert await asyncio.wait_for(replacement.wait(), 5) == 0

            final = await rows()
            assert set(received_ids) == {row[0] for row in final}
            assert len(received_ids) == count + (outcome == "unknown")
            assert all(row[3:] == (None, None) for row in final)
            async with database.connection() as conn:
                attempts = await (await conn.execute(
                    "select outcome from delivery_attempts where notification_id=%s order by attempt",
                    (first_id,),
                )).fetchall()
            assert attempts == ([("unknown",), ("delivered",)] if outcome == "unknown" else [("delivered",)])
        finally:
            reply.set()
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
