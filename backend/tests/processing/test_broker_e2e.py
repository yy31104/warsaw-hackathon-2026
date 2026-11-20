"""Exercise the actual broker consumer, DB workers and network receiver on an isolated queue."""

import asyncio
import json
import os
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import aio_pika
import httpx
import pytest

from alerting.config import files
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.ingest.amqp import consume
from alerting.rules.worker import process_batch
from .test_golden import ROOT


@contextmanager
def local_receiver():
    recorded = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            recorded.append((self.path, self.headers["Idempotency-Key"], payload["to"]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", recorded
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.e2e
async def test_workflow_from_real_rabbit_publish(database, monkeypatch):
    broker = os.getenv("INGEST_TEST_RABBITMQ_URL")
    if not broker:
        pytest.skip("set INGEST_TEST_RABBITMQ_URL to run the real RabbitMQ chain")
    source = next(s for s in files.sources() if s["id"] == "workflow-bus")
    body = (ROOT / "fixtures/events/workflow_failed.json").read_bytes()
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
                        "select id,channel,contact,state from notifications order by channel"
                    )
                ).fetchall()
                assert [(ch, state) for _, ch, _, state in outbox] == [
                    ("phone", "pending"),
                    ("sms", "pending"),
                    ("ui", "delivered"),
                ]
                assert await (
                    await conn.execute("select rule_id,occurrence_count from alerts")
                ).fetchall() == [("settlements-wf-failed", 1)]
            with local_receiver() as (test_url, recorded):
                real = os.getenv("E2E_FAKE_TELECOM_URL")
                monkeypatch.setenv("FAKE_TELECOM_URL", real or test_url)
                async with httpx.AsyncClient(trust_env=False) as client:
                    for ch in ("phone", "sms"):
                        assert await deliver_one(database, {ch: Telecom(ch, client)}, [ch])
                    if real:
                        messages = await client.get(real.rstrip("/") + "/messages")
                        messages.raise_for_status()
                        rows = messages.json()
                        if isinstance(rows, dict):
                            rows = rows.get("messages", rows.get("items", []))
                        assert {str(item[0]) for item in outbox if item[1] != "ui"} <= {
                            str(r.get("idempotency_key", r.get("notification_id"))) for r in rows
                        }
                    else:
                        assert set(recorded) == {
                            ("/call" if ch == "phone" else "/sms", str(nid), contact)
                            for nid, ch, contact, _ in outbox
                            if ch != "ui"
                        }
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
