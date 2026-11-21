"""Measure one controlled RabbitMQ-to-receiver saturation trial inside Compose."""

import argparse
import asyncio
import gzip
import json
import os
from collections import Counter
from pathlib import Path
from statistics import median

import aio_pika
import httpx
import psycopg

from loadtest.bench_collect import Collector, percentiles, reconcile
from loadtest.bench_generate import SOURCE, epoch_ms, publish, workload, workload_sha256

QUEUE = "events.workflow"
SAMPLE_INTERVAL = 2
LANES = ("normal", "critical")


async def sample(collector, channel):
    queue = await channel.declare_queue(QUEUE, passive=True, timeout=4)
    waits = await collector.query("""
        SELECT coalesce(wait_event_type, 'Running') AS type,
               coalesce(wait_event, 'CPU or runnable') AS event, count(*)::int AS sessions
        FROM pg_stat_activity
        WHERE datname=current_database() AND backend_type='client backend'
          AND pid<>pg_backend_pid() AND state='active'
        GROUP BY 1, 2 ORDER BY 1, 2
    """)
    metrics = await collector.progress()
    return {**metrics, "rabbit_ready": queue.declaration_result.message_count, "pg_activity": waits}


def describe_backlogs(samples, start_ms, duration_s, rate):
    """Compare medians in three consecutive parts of the steady offer window."""
    warmup = min(10, duration_s / 4)
    width = (duration_s - warmup) / 3
    bins = [[] for _ in range(3)]
    for row in samples:
        elapsed = (row["observed_at_ms"] - start_ms) / 1000
        if warmup <= elapsed < duration_s:
            bins[min(2, int((elapsed - warmup) / width))].append(row)
    noise = max(20, rate / 4)
    series = {}
    for field in ("pipeline", "rabbit_ready", "processing_backlog", "delivery_backlog"):
        if field == "pipeline":
            values = [[r["rabbit_ready"] + r["processing_backlog"] for r in b] for b in bins]
        else:
            values = [[r[field] for r in b] for b in bins]
        medians = [median(block) if block else None for block in values]
        if None in medians:
            status = "insufficient samples"
        else:
            early, middle, late = medians
            status = ("growing" if middle - early > noise and late - middle > noise
                      else "no sustained growth")
        series[field] = {"trend": status, "block_medians": medians}
    return {"warmup_s": warmup, "block_width_s": width, "minimum_growth_per_block": noise, **series}


def summarize(args, schedule, started, sampled_until, sent, snapshots, events, outbox,
              receipts, rejected, decisions, expected_slots):
    end = started + args.duration * 1000
    sending = Counter(row["status"] for row in sent)
    recorded = {row["source_event_id"] for row in events}
    verified = reconcile(expected_slots, outbox, receipts)
    arrivals = {r["idempotency_key"]: r["received_at"] for r in receipts}
    invalid = set(verified["invalid_receipts"])
    delays = {lane: [] for lane in LANES}
    for notice in outbox:
        arrival = arrivals.get(notice["id"])
        if arrival is not None and notice["id"] not in invalid:
            delays[notice["lane"]].append(arrival - notice["gen_sent_at_ms"])
    steady_start = started + min(10, args.duration / 4) * 1000
    accepted_window = sum(started <= r["received_at_ms"] < end for r in events)
    processed_window = sum(started <= r["processed_at_ms"] < end
                           for r in events if r["processed_at_ms"] is not None)
    processed_steady = sum(steady_start <= r["processed_at_ms"] < end
                           for r in events if r["processed_at_ms"] is not None)
    processed_final = sum(r["processed_at_ms"] is not None for r in events)
    confirmed_window = sum(started <= r.get("broker_confirmed_at_ms", 0) < end for r in sent)
    wait_samples = Counter()
    max_waits = Counter()
    for row in snapshots:
        if not started <= row["observed_at_ms"] < end:
            continue
        for wait in row["pg_activity"]:
            key = wait["type"] + ":" + wait["event"]
            wait_samples[key] += wait["sessions"]
            max_waits[key] = max(max_waits[key], wait["sessions"])
    backlog = describe_backlogs(snapshots, started, args.duration, args.normal_rate + args.critical_rate)
    checks = []
    expected = args.duration * (args.normal_rate + args.critical_rate)
    if len(sent) != expected:
        checks.append(f"Generator ledger has {len(sent)} rows instead of {expected}.")
    if args.duration >= 60 and backlog["pipeline"]["trend"] == "insufficient samples":
        checks.append("The steady offer window lacks backlog observations.")
    if not snapshots:
        checks.append("No database samples were collected.")
    return {
        "run_id": args.run_id,
        "status": "FAIL" if checks else "PASS",
        "measurement_errors": checks,
        "duration_s": args.duration,
        "normal_workers": args.normal_workers,
        "ingest_workers": args.ingest_workers,
        "normal_keys": args.normal_keys,
        "normal_offer_per_s": args.normal_rate,
        "critical_offer_per_s": args.critical_rate,
        "started_at_ms": started,
        "offer_ended_at_ms": end,
        "sampled_until_ms": sampled_until,
        "workload_sha256": workload_sha256(schedule),
        "scheduled": len(sent),
        "broker_confirmed": sending["confirmed"],
        "broker_confirmed_in_window": confirmed_window,
        "generator_dropped": sending["dropped"],
        "publish_errors": sending["error"],
        "max_abs_offer_lag_ms": max(abs(r["gen_sent_at_ms"] - started - r["scheduled_offset_ms"])
                                    for r in sent),
        "p99_abs_offer_lag_ms": percentiles(
            abs(r["gen_sent_at_ms"] - started - r["scheduled_offset_ms"]) for r in sent
        )["p99_ms"],
        "accepted_in_window": accepted_window,
        "accepted_in_window_per_s": accepted_window / args.duration,
        "processed_in_window": processed_window,
        "processed_in_window_per_s": processed_window / args.duration,
        "processed_steady_per_s": processed_steady / ((end - steady_start) / 1000),
        "accepted_after_drain": len(recorded),
        "processed_after_drain": processed_final,
        "confirmed_not_stored_at_snapshot": sum(
            r["status"] == "confirmed" and r["event_id"] not in recorded for r in sent
        ),
        "rejected": rejected.get("REJECTED", 0),
        "duplicate_inputs": rejected.get("DUPLICATE", 0),
        "conflicting_inputs": rejected.get("CONFLICT", 0),
        "backlog": backlog,
        "final_sample": snapshots[-1] if snapshots else {},
        "peak_pipeline_backlog": max((r["rabbit_ready"] + r["processing_backlog"] for r in snapshots), default=0),
        "peak_delivery_backlog": max((r["delivery_backlog"] for r in snapshots), default=0),
        "peak_oldest_pending_s": max((r["oldest_pending_s"] for r in snapshots), default=0),
        "postgres_active_wait_session_samples": dict(wait_samples),
        "postgres_peak_waiting_sessions": dict(max_waits),
        "rule_decisions_after_drain": decisions,
        "notifications_expected": len(expected_slots),
        "notifications_verified": sum(map(len, delays.values())),
        "notification_slots_without_verified_receipt": verified["lost_count"],
        "duplicate_receipts": verified["duplicate_receiver_count"],
        "invalid_receipts": len(invalid),
        "receiver_latency": {lane: percentiles(delays[lane]) for lane in LANES},
    }


async def trial(args):
    schedule = list(workload(Path("fixtures/events/workflow_failed.json"), args.tag,
                             args.duration, args.normal_rate, args.critical_rate,
                             normal_keys=args.normal_keys))
    expected = args.duration * (args.normal_rate + args.critical_rate)
    expected_slots = {(row.step, channel) for batch in schedule for row in batch
                      for channel in ("sms", "phone")}
    args.output.mkdir(parents=True, exist_ok=True)
    print(f"RUN ID {args.run_id}", flush=True)
    print(f"Offer {args.normal_rate:,} normal/s + {args.critical_rate}/s critical for {args.duration}s "
          f"| {args.normal_workers} normal workers | {args.normal_keys:,} normal keys", flush=True)
    async with (
        await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True,
                                                application_name="m11-observer") as db,
        await aio_pika.connect(os.environ["RABBITMQ_URL"], timeout=8) as broker,
        httpx.AsyncClient(base_url=os.environ["FAKE_TELECOM_URL"], timeout=15, trust_env=False) as gateway,
    ):
        collector = Collector(db, args.tag)
        sender = await broker.channel(publisher_confirms=True, on_return_raises=True)
        peek = await broker.channel()
        queue = await sender.declare_queue(QUEUE, durable=True)
        status = await gateway.get("/mode")
        status.raise_for_status()
        async with asyncio.timeout(20):
            while queue.declaration_result.consumer_count != args.ingest_workers:
                await asyncio.sleep(0.25)
                queue = await peek.declare_queue(QUEUE, passive=True, timeout=4)
        before = await sample(collector, peek)
        if before["accepted"] or before["rabbit_ready"]:
            raise RuntimeError(f"Expected a fresh inbox and an empty broker queue: {before}")
        snapshots = [before]
        loop = asyncio.get_running_loop()
        start = loop.time()
        last_tick = -10
        deadline = start + args.duration + args.drain_seconds
        print("Offer started. Backlog samples below are live.", flush=True)
        sending = asyncio.create_task(publish(sender, queue.name, schedule, args.max_in_flight))
        try:
            while True:
                snapshots.append(await sample(collector, peek))
                current = snapshots[-1]
                elapsed = loop.time() - start
                if elapsed - last_tick >= 9.5:
                    print(f"{elapsed:6.1f}s | accepted {current['accepted']:>7,}  "
                          f"processed {current['processed']:>7,}  "
                          f"broker ready {current['rabbit_ready']:>7,}  "
                          f"DB pending {current['processing_backlog']:>7,}", flush=True)
                    last_tick = elapsed
                if elapsed >= args.duration and sending.done() and (
                    current["processed"] == expected
                    and current["terminal"] >= len(expected_slots)
                    and not current["delivery_backlog"]
                ):
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(min(SAMPLE_INTERVAL, max(0, deadline - loop.time())))
            started_ms, sent = await sending
        finally:
            if not sending.done():
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
        async with db.transaction():
            await db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            sampled_until = epoch_ms()
            events, outbox, attempts, decisions = await collector.evidence()
            rejects = {row["kind"]: row["count"] for row in await collector.query(
                "SELECT kind,count(*) FROM ingest_rejections WHERE source_id=%s GROUP BY kind", (SOURCE,)
            )}
        receipt_response = await gateway.get("/messages")
        receipt_response.raise_for_status()
        receipts = [row for row in receipt_response.json()["items"] if row["received_at"] <= sampled_until]
    result = summarize(args, schedule, started_ms, sampled_until, sent, snapshots, events, outbox,
                       receipts, rejects, decisions, expected_slots)
    result["evidence_written_at_ms"] = epoch_ms()
    with gzip.open(args.output / "observations.json.gz", "wt", encoding="utf-8", compresslevel=2) as raw:
        json.dump({"run_id": args.run_id, "sent": sent, "events": events, "outbox": outbox,
                   "attempts": attempts, "receiver_receipts": receipts, "samples": snapshots,
                   "rejections_by_kind": rejects}, raw, separators=(",", ":"), ensure_ascii=False)
    (args.output / "samples.json").write_text(json.dumps(snapshots, indent=2) + "\n", encoding="utf-8")
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"RUN ID {args.run_id} | {result['status']} measurement", flush=True)
    print(f"Inside offer: {result['accepted_in_window_per_s']:,.1f} ingested/s, "
          f"{result['processed_in_window_per_s']:,.1f} processed/s.", flush=True)
    print(f"Confirmed {result['broker_confirmed']:,}/{expected:,} | dropped {result['generator_dropped']:,} | "
          f"publish errors {result['publish_errors']:,} | rejected {result['rejected']:,}", flush=True)
    print(f"Pipeline backlog {result['backlog']['pipeline']['trend']} | "
          f"peak {result['peak_pipeline_backlog']:,} | "
          f"verified phone/SMS {result['notifications_verified']:,}/{len(expected_slots):,}", flush=True)
    for lane, latency in result["receiver_latency"].items():
        print(f"{lane.capitalize()} p50/p95/p99 (ms): "
              f"{latency['p50_ms']}/{latency['p95_ms']}/{latency['p99_ms']}  N={latency['n']}", flush=True)
    return result["status"] == "PASS"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--normal-workers", type=int, required=True)
    parser.add_argument("--ingest-workers", type=int, default=1)
    parser.add_argument("--normal-keys", type=int, required=True)
    parser.add_argument("--normal-rate", type=int, required=True)
    parser.add_argument("--critical-rate", type=int, default=2)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--drain-seconds", type=int, default=20)
    parser.add_argument("--max-in-flight", type=int, default=2000)
    options = parser.parse_args()
    if min(options.normal_workers, options.ingest_workers, options.normal_keys, options.duration,
           options.max_in_flight) < 1:
        parser.error("Worker and key counts, duration, and in-flight limit must be positive.")
    if min(options.normal_rate, options.critical_rate, options.drain_seconds) < 0 or (
        options.normal_rate + options.critical_rate == 0
    ):
        parser.error("Rates and drain time must be nonnegative and total load must be positive.")
    raise SystemExit(0 if asyncio.run(trial(options)) else 1)
