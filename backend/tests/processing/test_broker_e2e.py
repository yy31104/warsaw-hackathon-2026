"""Exercise the actual broker consumer, DB workers and network receiver on an isolated queue."""

import asyncio
import os
import socket
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import aio_pika
import httpx
import pytest
import uvicorn

from alerting.config import files
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.ingest.amqp import consume
from alerting.rules.worker import process_batch
from .test_golden import GOLD, ROOT


@asynccontextmanager
async def telecom_receiver(tmp_path, monkeypatch):
    external = os.getenv("E2E_FAKE_TELECOM_URL")
    if external:
        yield external.rstrip("/")
        return

    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setenv("FAKE_TELECOM_DB", str(tmp_path / "receipts.sqlite3"))
    from mocks.fake_telecom.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(), log_config=None, access_log=False))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail("fake-telecom stopped before accepting requests")
                    await asyncio.sleep(0.025)
            yield url
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)


@pytest.mark.e2e
async def test_workflow_from_real_rabbit_publish(database, monkeypatch, tmp_path):
    broker = os.getenv("INGEST_TEST_RABBITMQ_URL")
    if not broker:
        pytest.skip("set INGEST_TEST_RABBITMQ_URL to run the real RabbitMQ chain")
    case = next(c for c in GOLD["cases"] if c["id"] == "settlements-positive")
    source = next(s for s in files.sources() if s["id"] == case["send"]["source"])
    body = (ROOT / case["send"]["file"]).read_bytes()
    key = "broker-e2e-" + uuid4().hex
    source = dict(source, queue=key)
    async with await aio_pika.connect(broker, timeout=5) as connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(key, durable=True)
        worker = asyncio.create_task(consume(channel, [source], database))
        try:
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body, message_id=key, delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                ),
                routing_key=key,
            )
            async with asyncio.timeout(10):
                while True:
                    if worker.done():
                        await worker
                    async with database.connection() as conn:
                        row = await (
                            await conn.execute(
                                "select status,fields->>'$error_code' from events where source_event_id=%s",
                                (key,),
                            )
                        ).fetchone()
                    if row:
                        break
                    await asyncio.sleep(0.025)
            assert row == ("pending", "TIMEOUT")
            assert await process_batch(database, batch=1) == 1
            async with database.connection() as conn:
                outbox = await (
                    await conn.execute(
                        "select id,channel,state,recipient from notifications order by channel"
                    )
                ).fetchall()
                assert [(ch, state) for _, ch, state, _ in outbox] == [
                    ("phone", "pending"),
                    ("sms", "pending"),
                    ("ui", "delivered"),
                ]
                assert await (
                    await conn.execute("select rule_id,occurrence_count from alerts")
                ).fetchall() == [(case["expect"]["alert"]["rule"], 1)]
            assert {(recipient, ch) for _, ch, _, recipient in outbox} == {
                (n["recipient"], n["channel"]) for n in case["expect"]["notifications"]
            }
            directory = {user["id"]: user for user in files.directory()["users"]}
            expected = {
                str(nid): (ch, directory[recipient]["contacts"][ch])
                for nid, ch, _, recipient in outbox
                if ch != "ui"
            }
            async with telecom_receiver(tmp_path, monkeypatch) as receiver:
                monkeypatch.setenv("FAKE_TELECOM_URL", receiver)
                async with httpx.AsyncClient(trust_env=False) as client:
                    for ch in ("phone", "sms"):
                        assert await deliver_one(database, {ch: Telecom(ch, client)}, [ch])
                    messages = await client.get(receiver + "/messages")
                    messages.raise_for_status()
                    receipts = [
                        (r["idempotency_key"], r["channel"], r["to"])
                        for r in messages.json()["items"]
                        if r["idempotency_key"] in expected
                    ]
                    assert sorted(receipts) == sorted(
                        (key, *value) for key, value in expected.items()
                    )
            async with database.connection() as conn:
                assert await (await conn.execute("select status from events")).fetchall() == [
                    ("processed",)
                ]
                assert await (
                    await conn.execute("select count(*) from notifications where state='delivered'")
                ).fetchone() == (3,)
                assert await (
                    await conn.execute(
                        "select count(*) from delivery_attempts where outcome='delivered'"
                    )
                ).fetchone() == (2,)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await queue.delete(if_unused=False, if_empty=False)
