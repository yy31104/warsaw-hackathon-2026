"""Run a repeatable AMQP benchmark inside an isolated Alert Funnel Compose stack."""

import argparse
import asyncio
import gzip
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import aio_pika
import httpx
import psycopg

from alerting.config import files
from loadtest.bench_collect import Collector, reconcile, throughput
from loadtest.bench_generate import SOURCE, epoch_ms, publish, workload, workload_sha256


async def gateway_mode(gateway):
    try:
        response = await gateway.get("/mode", timeout=0.5)
        response.raise_for_status()
        return response.json()["mode"]
    except httpx.RequestError:
        return None


async def receipts(gateway):
    response = await gateway.get("/messages")
    response.raise_for_status()
    return response.json()["items"]


async def observe(args, collector, gateway, channel, queue, schedule, expected_count, expected_slots):
    if (await collector.progress())["accepted"]:
        raise RuntimeError(f"Existing benchmark rows for {args.tag}; use an empty benchmark inbox")
    mode = await gateway_mode(gateway)
    if args.stopped_provider_seconds:
        if mode is not None:
            raise RuntimeError("The gateway was still reachable when the container-stop trial started")
        # The host harness supplies a new provider volume and checks its empty inbox before stopping it.
        prior_receipts = set()
        outage = {"kind": "stopped_container", "requested_s": args.stopped_provider_seconds,
                  "first_unreachable_at_ms": epoch_ms(), "restored_mode": None}
    else:
        if mode != "ok":
            raise RuntimeError(f"Receiver must be in ok mode at the start; found {mode}")
        prior_receipts = {receipt["idempotency_key"] for receipt in await receipts(gateway)}
        outage = None
    loop = asyncio.get_running_loop()
    samples = []
    deadline = loop.time() + args.duration + args.stopped_provider_seconds + args.drain_timeout
    upload = asyncio.create_task(publish(channel, queue, schedule, args.max_in_flight))
    try:
        while True:
            progress = await collector.progress()
            samples.append(progress)
            if outage and outage["restored_mode"] is None:
                # The last unreachable probe brackets the server's actual restart independently of Docker.
                mode = await gateway_mode(gateway)
                if mode is None:
                    outage.update(while_down=progress, last_unreachable_at_ms=epoch_ms())
                else:
                    outage.update(restored_mode=mode, first_reachable_at_ms=epoch_ms())
            if (outage is None or outage["restored_mode"] == "ok") and upload.done() and (
                progress["accepted"] == expected_count and progress["processed"] == expected_count
                and progress["terminal"] >= len(expected_slots) and not progress["delivery_backlog"]
            ):
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.5)
        started_ms, sent = await upload
        events, outbox, attempts, decisions = await collector.evidence()
        received = [item for item in await receipts(gateway) if item["idempotency_key"] not in prior_receipts]
    finally:
        if not upload.done():
            upload.cancel()
            await asyncio.gather(upload, return_exceptions=True)

    if outage:
        outage["receipts_while_down"] = [
            item for item in received if item["received_at"] <= outage.get("last_unreachable_at_ms", 0)
        ]

    fallback = await collector.verified_fallbacks(outbox, f"http://{os.getenv('SMTP_HOST', 'mailpit')}:8025")
    evidence = {
        "started_at_ms": started_ms, "sent": sent, "stored": events, "outbox": outbox,
        "attempts": attempts, "receiver_receipts": received, "samples": samples, "outage": outage,
        "fallback": fallback,
    }
    return evidence, decisions


def summarize(args, schedule, evidence, decisions, expected_slots):
    sent = evidence["sent"]
    events = evidence["stored"]
    outbox = evidence["outbox"]
    counts = Counter(row["status"] for row in sent)
    expected_ids = {row["event_id"] for row in sent}
    stored_ids = {row["source_event_id"] for row in events}
    expected_times = {row["event_id"]: row["gen_sent_at_ms"] for row in sent}
    trace_mismatches = [
        row["source_event_id"] for row in events
        if expected_times.get(row["source_event_id"]) != row["gen_sent_at_ms"]
    ]
    summaries = reconcile(expected_slots, outbox, evidence["receiver_receipts"])
    rates = throughput(evidence["started_at_ms"], args.duration, events, outbox, evidence["receiver_receipts"])
    fallback = evidence["fallback"]
    unconfirmed_originals = set(summaries["missing_receiver_ids"]) | set(summaries["invalid_receipts"])
    problems = []
    if counts["confirmed"] != len(sent):
        problems.append("Some scheduled events were not broker-confirmed")
    if expected_ids != stored_ids or rates["processed"]["count"] != len(sent):
        problems.append("Committed and processed event IDs did not reconcile to the schedule")
    if trace_mismatches:
        problems.append("Stored generation timestamps differed from the generator")
    if any(summaries[key] for key in (
        "lost_count", "unexpected_plan_slots", "duplicate_plan_slots", "unexpected_receiver_ids",
        "missing_receiver_ids", "unconfirmed_outbox_ids", "invalid_receipts", "duplicate_receiver_count",
    )):
        problems.append("Outbox and receiver receipts did not reconcile")
    outage = evidence["outage"]
    recovered_retries = {
        row["id"] for row in outbox if row["state"] == "delivered" and row["attempts"] > 1
    }
    if outage and (
        outage["restored_mode"] != "ok" or outage["receipts_while_down"]
        or outage.get("while_down", {}).get("delivery_backlog") != len(expected_slots)
        or not any(attempt["outcome"] in ("retryable", "unknown") and
                   attempt["finished_at_ms"] is not None and
                   attempt["finished_at_ms"] <= outage.get("last_unreachable_at_ms", 0)
                   for attempt in evidence["attempts"])
        or max(expected_times.values()) > outage.get("last_unreachable_at_ms", 0)
    ):
        problems.append("The gateway did not demonstrate the full outage and recovery")
    return {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "normal_workers": args.normal_workers,
        "critical_workers": args.critical_workers,
        "workload_tag": args.tag,
        "workload_sha256": workload_sha256(schedule),
        "duration_s": args.duration,
        "offered_normal_events_per_s": args.normal_rate,
        "offered_critical_events_per_s": args.critical_rate,
        "normal_grouping": "per event" if args.stopped_provider_seconds else "one hot key",
        "scheduled": len(sent),
        "broker_confirmed": counts["confirmed"],
        "dropped": counts["dropped"],
        "publish_errors": counts["error"],
        "missing_event_ids": sorted(expected_ids - stored_ids),
        "generation_trace_mismatches": sorted(trace_mismatches),
        "max_absolute_schedule_lag_ms": max(
            abs(row["gen_sent_at_ms"] - evidence["started_at_ms"] - row["scheduled_offset_ms"]) for row in sent
        ),
        "throughput": rates,
        "peak_processing_backlog": max(sample["processing_backlog"] for sample in evidence["samples"]),
        "peak_delivery_backlog": max(sample["delivery_backlog"] for sample in evidence["samples"]),
        "peak_oldest_pending_s": max(sample["oldest_pending_s"] for sample in evidence["samples"]),
        "decisions": decisions,
        "decisions_per_1000": decisions * 1000 / len(events) if events else None,
        "sms_phone_receipts_per_1000": summaries["e2e"]["n"] * 1000 / len(events) if events else None,
        "receiver": summaries,
        "outage": outage,
        "telecom_recovered_after_retry_ids": sorted(recovered_retries),
        "fallback": {
            "replacement_count": len(fallback["plans"]),
            "confirmed_email": len(fallback["mailpit_receipts"]),
            "confirmed_ui": sum(row["channel"] == "ui" and row["state"] == "delivered" for row in fallback["plans"]),
            "confirmed_original_ids": fallback["confirmed_original_ids"],
            "loss_count_after_verified_replacements": len(summaries["missing_plan_slots"]) +
                len(unconfirmed_originals - set(fallback["confirmed_original_ids"])),
        },
    }


async def run(args):
    schedule = list(workload(
        args.fixture, args.tag, args.duration, args.normal_rate, args.critical_rate,
        unique_normal=bool(args.stopped_provider_seconds),
    ))
    expected_count = sum(len(batch) for batch in schedule)
    expected_slots = {(send.step, channel) for batch in schedule for send in batch for channel in ("sms", "phone")}
    source = next(source for source in files.sources() if source["id"] == SOURCE)
    queue = source["queue"]
    async with (
        await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True) as db,
        await aio_pika.connect(os.environ["RABBITMQ_URL"], timeout=10) as broker,
        httpx.AsyncClient(base_url=os.environ["FAKE_TELECOM_URL"], timeout=10, trust_env=False) as gateway,
    ):
        collector = Collector(db, args.tag)
        channel = await broker.channel(publisher_confirms=True, on_return_raises=True)
        await channel.get_queue(queue, ensure=True)
        evidence, decisions = await observe(args, collector, gateway, channel, queue, schedule, expected_count, expected_slots)
    summary = summarize(args, schedule, evidence, decisions, expected_slots)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with gzip.open(args.output / "raw.json.gz", "wt", encoding="utf-8") as file:
        json.dump(evidence, file, separators=(",", ":"))
        file.write("\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "receiver"}, indent=2))
    print(f'E2E: {summary["receiver"]["e2e"]}; lost: {summary["receiver"]["lost_count"]}')
    return 0 if summary["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=Path("fixtures/events/workflow_failed.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="Short synthetic ID prefix, reused for identical cohorts")
    parser.add_argument("--duration", type=int, default=20, help="Offered workload duration in seconds")
    parser.add_argument("--normal-rate", type=int, default=700, help="Normal events offered per second")
    parser.add_argument("--critical-rate", type=int, default=2, help="Critical events offered per second")
    parser.add_argument("--normal-workers", type=int, required=True, help="Record the actual scale from Compose")
    parser.add_argument("--critical-workers", type=int, default=1, help="Record the actual scale from Compose")
    parser.add_argument("--stopped-provider-seconds", type=int, default=0,
                        help="Measure the externally stopped gateway; use run-platform-bench.ps1 for the stop/start")
    parser.add_argument("--drain-timeout", type=int, default=90, help="Maximum wait for processing and delivery after the trial")
    parser.add_argument("--max-in-flight", type=int, default=2000, help="Publish above this in-flight limit as explicit drops")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", args.tag):
        parser.error("--tag needs 1–41 lowercase letters, digits or hyphens")
    if min(args.duration, args.normal_workers, args.critical_workers, args.drain_timeout, args.max_in_flight) < 1:
        parser.error("duration, worker counts, drain timeout and in-flight limit must be positive")
    if min(args.normal_rate, args.critical_rate, args.stopped_provider_seconds) < 0 or not (args.normal_rate or args.critical_rate):
        parser.error("rates and outage must be nonnegative and total offered rate must be positive")
    if args.stopped_provider_seconds and args.duration >= args.stopped_provider_seconds:
        parser.error("the outage trial's entire offered workload must fit inside the outage")
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(run(args), loop_factory=factory)


if __name__ == "__main__":
    raise SystemExit(main())
