"""Reproduce the same synthetic workflow inputs and open-loop AMQP schedule."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from time import time_ns

import aio_pika
from pamqp.commands import Basic

TICKS_PER_SECOND = 20
SOURCE = "workflow-bus"


def epoch_ms():
    return time_ns() // 1_000_000


@dataclass(frozen=True)
class Send:
    event_id: str
    step: str
    lane: str
    payload: bytes


def workload(fixture, tag, duration, normal_rate, critical_rate, *, unique_normal=False):
    template = json.loads(fixture.read_bytes())
    counts = {"normal": 0, "critical": 0}
    for tick in range(duration * TICKS_PER_SECOND):
        batch = []
        for lane, rate in (("normal", normal_rate), ("critical", critical_rate)):
            count = (tick + 1) * rate // TICKS_PER_SECOND - tick * rate // TICKS_PER_SECOND
            for _ in range(count):
                counts[lane] += 1
                event_id = f"{tag}-{lane}-{counts[lane]:06d}"
                step = f"{tag}-hot" if lane == "normal" and not unique_normal else event_id
                event = dict(template, workflowId=event_id, step=step,
                             severity="ERROR" if lane == "normal" else "FATAL")
                body = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                batch.append(Send(event_id, step, lane, body))
        yield batch


def workload_sha256(schedule):
    digest = hashlib.sha256()
    for tick, batch in enumerate(schedule):
        for event in batch:
            digest.update(f"{tick}\t{event.event_id}\t".encode() + event.payload + b"\n")
    return digest.hexdigest()


async def publish(channel, queue, schedule, max_in_flight):
    """Offer according to the wall-clock schedule, independently of confirmation latency."""
    exchange = channel.default_exchange
    loop = asyncio.get_running_loop()
    pending = set()
    records = []

    async def send(event, row):
        try:
            confirmation = await exchange.publish(
                aio_pika.Message(
                    event.payload, message_id=event.event_id, content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers={"x-gen-sent-at": row["gen_sent_at_ms"]},
                ),
                routing_key=queue, mandatory=True, timeout=10,
            )
            if not isinstance(confirmation, Basic.Ack):
                raise RuntimeError(f"broker returned {type(confirmation).__name__}")
            row.update(status="confirmed", broker_confirmed_at_ms=epoch_ms())
        except Exception as exc:
            row.update(status="error", error=f"{type(exc).__name__}: {exc}")

    started_ms = epoch_ms()
    started = loop.time()
    for tick, batch in enumerate(schedule):
        offset_s = tick / TICKS_PER_SECOND
        await asyncio.sleep(max(0, started + offset_s - loop.time()))
        offered_ms = epoch_ms()
        for event in batch:
            row = {
                "event_id": event.event_id, "step": event.step, "lane": event.lane,
                "scheduled_offset_ms": int(offset_s * 1000), "gen_sent_at_ms": offered_ms,
            }
            records.append(row)
            if len(pending) >= max_in_flight:
                row["status"] = "dropped"
                continue
            task = asyncio.create_task(send(event, row))
            pending.add(task)
            task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending)
    return started_ms, records
