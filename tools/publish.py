"""Send a sample JSON event to a configured RabbitMQ or HTTP source."""

import argparse
import asyncio
import json
import math
import os
from collections.abc import Iterator
from itertools import repeat
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import aio_pika
import httpx

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


async def outgoing(bodies: Iterator[bytes], message_id: str | None, rate: float | None):
    loop = asyncio.get_running_loop()
    next_send = loop.time()
    for body in bodies:
        if rate is not None:
            await asyncio.sleep(max(0, next_send - loop.time()))
            next_send = loop.time() + 1 / rate
        yield body, message_id or uuid4().hex, int(SYSTEM.now().timestamp() * 1000)


async def publish(queue: str, bodies: Iterator[bytes], message_id: str | None, rate: float | None):
    url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    confirmed = 0
    last_id = None
    try:
        async with await aio_pika.connect(url, timeout=10) as connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.declare_queue(queue, durable=True)
            async for body, current_id, sent_at in outgoing(bodies, message_id, rate):
                message = aio_pika.Message(
                    body,
                    message_id=current_id,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers={"x-gen-sent-at": sent_at},
                )
                await channel.default_exchange.publish(message, routing_key=queue, mandatory=True, timeout=10)
                confirmed += 1
                last_id = current_id
    except (aio_pika.exceptions.AMQPError, aio_pika.exceptions.ChannelInvalidStateError, OSError) as exc:
        raise RuntimeError(f"RabbitMQ {type(exc).__name__} on {queue} ({confirmed} confirmed)") from exc
    return confirmed, last_id


async def publish_http(source, bodies, message_id, rate):
    base = os.getenv("ALERTING_API_URL", "http://localhost:8000").rstrip("/")
    url = f"{base}/v1/events/{quote(source['id'], safe='')}"
    if not source.get("api_key"):
        raise ValueError("HTTP source has no configured API key")
    headers = {"X-Api-Key": source["api_key"], "Content-Type": "application/json"}
    accepted = duplicates = 0
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        async for body, key, sent_at in outgoing(bodies, message_id, rate):
            response = await client.post(url, content=body, headers={
                "Idempotency-Key": key, "X-Gen-Sent-At": str(sent_at),
            })
            if response.status_code != 202:
                raise RuntimeError(f"HTTP {response.status_code} from {source['id']} for {key}")
            receipt = response.json()
            if receipt["conflicts"] or receipt["rejected"]:
                raise RuntimeError(f"HTTP ingest for {key}: conflicts={receipt['conflicts']} "
                                   f"rejected={len(receipt['rejected'])}")
            accepted += receipt["accepted"]
            duplicates += receipt["duplicates"]
    return accepted, duplicates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source ID from config/sources.yaml")
    parser.add_argument("--via", choices=("amqp", "http"), help="defaults to the configured source transport")
    parser.add_argument("--file", required=True, type=Path, help="path to one JSON event")
    parser.add_argument("--count", type=int, default=1, help="number of messages (default: 1)")
    parser.add_argument("--message-id", help="reuse this message ID for every copy (default: a new ID per copy)")
    parser.add_argument("--rate", type=float, help="maximum messages per second")
    parser.add_argument("--unique", metavar="FIELD", help="append a unique suffix to this top-level string field per copy")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    if args.rate is not None and (not math.isfinite(args.rate) or args.rate <= 0):
        parser.error("--rate must be positive and finite")
    if args.message_id is not None and (not args.message_id.strip() or len(args.message_id.encode()) > 255):
        parser.error("--message-id must be nonblank and at most 255 UTF-8 bytes")

    sources = {s["id"]: s for s in files.sources() if s["transport"] in ("rabbitmq", "http")}
    if args.source not in sources:
        parser.error(f"unknown publisher source {args.source!r}. Available: {', '.join(sources)}")
    source = sources[args.source]
    via = "amqp" if source["transport"] == "rabbitmq" else "http"
    if args.via and args.via != via:
        parser.error(f"{args.source} uses {via}")
    if via == "http" and args.message_id and (
        not args.message_id.isascii() or not args.message_id.isprintable() or args.message_id != args.message_id.strip()
    ):
        parser.error("an HTTP message ID must be printable ASCII without leading or trailing spaces")
    try:
        with args.file.open("rb") as sample:
            bodies = event_bodies(sample.read(MAX_BYTES + 1), args.count, args.unique)
        if via == "http":
            accepted, duplicates = asyncio.run(publish_http(source, bodies, args.message_id, args.rate))
            print(f"HTTP {args.source}: accepted={accepted} duplicates={duplicates}")
        else:
            confirmed, last_id = asyncio.run(publish(source["queue"], bodies, args.message_id, args.rate))
            print(f"RabbitMQ confirmed {confirmed} on {source['queue']} (last message_id={last_id})")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    except (RuntimeError, httpx.RequestError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
