import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aio_pika
import psycopg
import pytest
import yaml
from aio_pika import DeliveryMode, Message
from psycopg.conninfo import make_conninfo

from alerting.clock import SYSTEM
from alerting.ingest.amqp import store_and_ack

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def worker(env, log_file):
    with log_file.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "alerting.ingest.amqp"], env=env,
                                   cwd=ROOT / "backend", stdout=log, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


async def test_failed_database_transaction_acknowledges_nothing(inbox_pool):
    async with inbox_pool.connection() as conn:
        await conn.execute("ALTER TABLE events ADD CHECK (source_event_id <> 'db-fault')")
    source = {"id": "test-queue", "transport": "rabbitmq"}
    batch = [(source, SimpleNamespace(body=b'{"x":1}', message_id=key, headers={}, ack=AsyncMock()), SYSTEM.now())
             for key in ("good", "db-fault")]
    with pytest.raises(psycopg.errors.CheckViolation):
        await store_and_ack(batch, [], inbox_pool)
    for _, message, _ in batch:
        message.ack.assert_not_awaited()
    async with inbox_pool.connection() as conn:
        assert await (await conn.execute("SELECT count(*) FROM events")).fetchone() == (0,)


async def test_live_broker_crash_recovery_and_retransmissions(inbox_pool, tmp_path):
    url = os.getenv("INGEST_TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("set INGEST_TEST_RABBITMQ_URL for the live broker/process crash test")
    source_id = "test-rabbit-" + uuid4().hex
    source = {"id": source_id, "transport": "rabbitmq", "queue": source_id}
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump({"sources": [source]}), encoding="utf-8")
    (tmp_path / "schemas.yaml").write_bytes((ROOT / "config/schemas.yaml").read_bytes())
    async with inbox_pool.connection() as conn:
        schema, = await (await conn.execute("SELECT current_schema()")).fetchone()
    env = {**os.environ, "CONFIG_DIR": str(tmp_path), "RABBITMQ_URL": url,
           "DATABASE_URL": make_conninfo(inbox_pool.conninfo, options=f"-c search_path={schema},public"),
           "FAULT_AFTER_COMMIT_BEFORE_ACK": "0"}
    original = (ROOT / "fixtures/events/workflow_failed.json").read_bytes()
    async with await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(source_id, durable=True)

        async def publish(key, body=original):
            await channel.default_exchange.publish(Message(body, message_id=key, delivery_mode=DeliveryMode.PERSISTENT,
                headers={"x-gen-sent-at": 1792833245000}), routing_key=queue.name)

        try:
            await publish("crash-window")
            crash_log = tmp_path / "crash.log"
            with worker(dict(env, FAULT_AFTER_COMMIT_BEFORE_ACK="1"), crash_log) as process:
                assert await asyncio.to_thread(process.wait, 30) == 1, crash_log.read_text(errors="replace")
            async with inbox_pool.connection() as conn:
                row = await (await conn.execute("SELECT source_event_id, fields FROM events")).fetchone()
                assert row is not None, crash_log.read_text(errors="replace")
                assert row[0] == "crash-window" and row[1]["$error_code"] == "TIMEOUT"

            # The process exited after COMMIT. The broker must still own the unacked delivery.
            async with asyncio.timeout(10):
                while (redelivery := await queue.get(fail=False)) is None:
                    await asyncio.sleep(0.05)
            assert redelivery.redelivered and redelivery.message_id == "crash-window"
            await redelivery.reject(requeue=True)

            resumed_log = tmp_path / "resumed.log"
            with worker(env, resumed_log) as resumed:
                for _ in range(10):
                    await asyncio.gather(*(publish("repeat-1000") for _ in range(100)))
                await publish("real-a")
                await publish("real-b")
                await publish("real-a", json.dumps(dict(json.loads(original), severity="FATAL")).encode())
                await publish("bad-json", b"broken")

                async with asyncio.timeout(35):
                    while True:
                        assert resumed.poll() is None, resumed_log.read_text(errors="replace")
                        async with inbox_pool.connection() as conn:
                            rows = await (await conn.execute(
                                "SELECT source_event_id, kind, count(*) FROM ingest_rejections GROUP BY 1, 2"
                            )).fetchall()
                        if sum(row[2] for row in rows) == 1002:
                            break
                        await asyncio.sleep(0.05)
                assert {(key, kind): n for key, kind, n in rows} == {
                    ("crash-window", "DUPLICATE"): 1, ("repeat-1000", "DUPLICATE"): 999,
                    ("real-a", "CONFLICT"): 1, ("bad-json", "REJECTED"): 1,
                }
                async with inbox_pool.connection() as conn:
                    events = await (await conn.execute("SELECT source_event_id, severity, trace FROM events")).fetchall()
                    assert {r[0] for r in events} == {"crash-window", "repeat-1000", "real-a", "real-b"}
                    assert all(severity == "error" and trace["gen_sent_at"] == 1792833245000 for _, severity, trace in events)
        finally:
            await queue.delete(if_unused=False, if_empty=False)
