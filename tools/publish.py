"""Send a sample JSON event to a configured RabbitMQ source."""

import argparse
import asyncio
import json
import math
import os
from collections.abc import Iterator
from itertools import repeat
from pathlib import Path
from uuid import uuid4

import aio_pika

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.ingest.normalize import MAX_BYTES, decode_json


def event_bodies(raw: bytes, count: int, unique: str | None = None) -> Iterator[bytes]:
    event = decode_json(raw)
    if not isinstance(event, dict):
        raise ValueError("expected one JSON object")
    if unique is None:
        return repeat(raw, count)

    prefix = event.get(unique)
    if not isinstance(prefix, str):
        raise ValueError(f"--unique needs an existing top-level string field: {unique}")

    def variants():
        for _ in range(count):
            copy = {**event, unique: f"{prefix}-{uuid4().hex}"}
            body = json.dumps(copy, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(body) > MAX_BYTES:
                raise ValueError("generated event exceeds 256 KiB")
            yield body

    return variants()


async def publish(queue: str, bodies: Iterator[bytes], message_id: str | None, rate: float | None):
    url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    confirmed = 0
    last_id = None
    try:
        async with await aio_pika.connect(url, timeout=10) as connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.declare_queue(queue, durable=True)
            loop = asyncio.get_running_loop()
            next_send = loop.time()
            for body in bodies:
                if rate is not None:
                    await asyncio.sleep(max(0, next_send - loop.time()))
                    next_send = loop.time() + 1 / rate
                current_id = message_id or uuid4().hex
                message = aio_pika.Message(
                    body,
                    message_id=current_id,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers={"x-gen-sent-at": int(SYSTEM.now().timestamp() * 1000)},
                )
                await channel.default_exchange.publish(message, routing_key=queue, mandatory=True, timeout=10)
                confirmed += 1
                last_id = current_id
    except (aio_pika.exceptions.AMQPError, aio_pika.exceptions.ChannelInvalidStateError, OSError) as exc:
        raise RuntimeError(f"RabbitMQ {type(exc).__name__} on {queue} ({confirmed} confirmed)") from exc
    return confirmed, last_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="RabbitMQ source ID from config/sources.yaml")
    parser.add_argument("--file", required=True, type=Path, help="path to one JSON event")
    parser.add_argument("--count", type=int, default=1, help="number of messages (default: 1)")
    parser.add_argument("--message-id", help="reuse this AMQP ID for every copy (default: a new ID per copy)")
    parser.add_argument("--rate", type=float, help="maximum messages per second")
    parser.add_argument("--unique", metavar="FIELD", help="append a unique suffix to this top-level string field per copy")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    if args.rate is not None and (not math.isfinite(args.rate) or args.rate <= 0):
        parser.error("--rate must be positive and finite")
    if args.message_id is not None and (not args.message_id.strip() or len(args.message_id.encode()) > 255):
        parser.error("--message-id must be nonblank and at most 255 UTF-8 bytes")

    queues = {s["id"]: s["queue"] for s in files.sources() if s["transport"] == "rabbitmq"}
    if args.source not in queues:
        parser.error(f"unknown RabbitMQ source {args.source!r}. Available: {', '.join(queues)}")
    try:
        with args.file.open("rb") as sample:
            bodies = event_bodies(sample.read(MAX_BYTES + 1), args.count, args.unique)
        confirmed, last_id = asyncio.run(publish(queues[args.source], bodies, args.message_id, args.rate))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    print(f"RabbitMQ confirmed {confirmed} on {queues[args.source]} (last message_id={last_id})")


if __name__ == "__main__":
    main()
