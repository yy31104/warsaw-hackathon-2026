"""Run the durable RabbitMQ inbox consumer with python -m alerting.ingest.amqp."""
import asyncio
import logging
import os
import sys
from functools import partial

import aio_pika
import psycopg

from alerting import db
from alerting.clock import SYSTEM
from alerting.config import files
from alerting.ingest.inputs import prepare
from alerting.ingest.store import save_batch

MAX_BATCH = 200
MAX_WAIT = 0.050
log = logging.getLogger(__name__)


async def store_and_ack(deliveries, schemas, db_pool):
    inputs = [prepare(message.body, source, schemas, message_id=message.message_id,
                      headers=message.headers, received_at=received_at)
              for source, message, received_at in deliveries]
    async with db_pool.connection() as conn:
        await save_batch(conn, inputs)
    # Test-only crash point. RabbitMQ redelivers the unacknowledged messages.
    if os.getenv("FAULT_AFTER_COMMIT_BEFORE_ACK") == "1":
        os._exit(1)
    for _, message, _ in deliveries:
        await message.ack()


async def consume(channel, sources, db_pool):
    inbox = asyncio.Queue(maxsize=MAX_BATCH)
    await channel.set_qos(prefetch_count=MAX_BATCH)

    async def receive(source, message):
        await inbox.put((source, message, SYSTEM.now()))

    for source in sources:
        queue = await channel.declare_queue(source["queue"], durable=True)
        await queue.consume(partial(receive, source))

    while True:
        batch = [await inbox.get()]
        try:
            async with asyncio.timeout(MAX_WAIT):
                while len(batch) < MAX_BATCH:
                    batch.append(await inbox.get())
        except TimeoutError:
            pass
        await store_and_ack(batch, files.schemas(), db_pool)


async def run():
    sources = [s for s in files.sources() if s["transport"] == "rabbitmq"]
    url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    await db.open_pool()
    try:
        while True:
            try:
                async with await aio_pika.connect_robust(url) as connection:
                    async with connection.channel() as channel:
                        await consume(channel, sources, db.pool())
            except (aio_pika.exceptions.AMQPError, aio_pika.exceptions.ChannelInvalidStateError,
                    psycopg.OperationalError, OSError):
                log.exception("Inbox connection interrupted; unacknowledged messages will be redelivered")
                await asyncio.sleep(1)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(), loop_factory=factory)
