"""Summarise one M11 end-to-end trial: lane latency, reconciliation and slowest-20 normal stage breakdown.

Usage: python loadtest/results/p99_batch_analyze.py <trial folder containing summary.json and observations.json.gz>
Reads only the files the trial wrote. Prints one JSON object.
"""

import gzip
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median


def main(folder):
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    obs = json.load(gzip.open(folder / "observations.json.gz"))
    events = {row["source_event_id"]: row for row in obs["events"]}
    # The notification's generation time is its triggering event's generation time (rendered meta), so the
    # triggering event is the generator row with the same alert step and generation millisecond.
    triggers = {}
    for row in obs["sent"]:
        triggers.setdefault((row["step"], row["gen_sent_at_ms"]), []).append(row["event_id"])
    first_attempt = {}
    for row in obs["attempts"]:
        started = row["started_at_ms"]
        if started is not None and (row["notification_id"] not in first_attempt
                                    or started < first_attempt[row["notification_id"]]):
            first_attempt[row["notification_id"]] = started
    receipts = Counter(r["idempotency_key"] for r in obs["receiver_receipts"])
    arrival = {}
    for r in obs["receiver_receipts"]:
        arrival[r["idempotency_key"]] = min(arrival.get(r["idempotency_key"], r["received_at"]), r["received_at"])
    expected = Counter(n["lane"] for n in obs["outbox"])
    received = Counter(n["lane"] for n in obs["outbox"] if n["id"] in arrival)
    duplicates = {lane: sum(receipts[n["id"]] - 1 for n in obs["outbox"] if n["lane"] == lane and receipts[n["id"]] > 1)
                  for lane in expected}
    rows = []
    unmatched = 0
    for n in obs["outbox"]:
        if n["lane"] != "normal" or n["id"] not in arrival:
            continue
        claim = first_attempt.get(n["id"])
        candidates = [events[e] for e in triggers.get((n["step"], n["gen_sent_at_ms"]), [])
                      if e in events and events[e]["processed_at_ms"] is not None
                      and claim is not None and events[e]["processed_at_ms"] <= claim]
        if not candidates:
            unmatched += 1
            continue
        event = max(candidates, key=lambda e: e["processed_at_ms"])
        rows.append({
            "notification_id": n["id"],
            "e2e_ms": arrival[n["id"]] - n["gen_sent_at_ms"],
            "offer_to_ingest_ms": event["received_at_ms"] - n["gen_sent_at_ms"],
            "ingest_to_processed_ms": event["processed_at_ms"] - event["received_at_ms"],
            "processed_to_first_claim_ms": claim - event["processed_at_ms"],
            "claim_to_receipt_ms": arrival[n["id"]] - claim,
        })
    rows.sort(key=lambda r: r["e2e_ms"], reverse=True)
    slow = rows[:20]
    stages = ("e2e_ms", "offer_to_ingest_ms", "ingest_to_processed_ms", "processed_to_first_claim_ms",
              "claim_to_receipt_ms")
    final = summary.get("final_sample", {})
    return {
        "run_id": summary["run_id"],
        "trial_status": summary["status"],
        "measurement_errors": summary["measurement_errors"],
        "workload_sha256": summary["workload_sha256"],
        "workers": {"normal": summary.get("actual_normal_workers"), "ingest": summary.get("actual_ingest_workers"),
                    "delivery": summary.get("actual_delivery_workers")},
        "receiver_latency": summary["receiver_latency"],
        "receipts": {lane: {"received": received[lane], "expected": expected[lane]} for lane in sorted(expected)},
        "notifications_expected": summary["notifications_expected"],
        "notifications_verified": summary["notifications_verified"],
        "slots_without_verified_receipt": summary["notification_slots_without_verified_receipt"],
        "duplicate_receipts": summary["duplicate_receipts"],
        "duplicate_receipts_by_lane": duplicates,
        "invalid_receipts": summary["invalid_receipts"],
        "events": {
            "scheduled": summary["scheduled"],
            "broker_confirmed": summary["broker_confirmed"],
            "generator_dropped": summary["generator_dropped"],
            "publish_errors": summary["publish_errors"],
            "accepted_after_drain": summary["accepted_after_drain"],
            "processed_after_drain": summary["processed_after_drain"],
            "accepted_not_processed_after_drain": summary["accepted_after_drain"] - summary["processed_after_drain"],
            "confirmed_not_stored_at_snapshot": summary["confirmed_not_stored_at_snapshot"],
            "final_processing_backlog": final.get("processing_backlog"),
            "final_delivery_backlog": final.get("delivery_backlog"),
            "final_rabbit_ready": final.get("rabbit_ready"),
            "final_failed": final.get("failed"),
        },
        "peak_delivery_backlog": summary["peak_delivery_backlog"],
        "peak_oldest_pending_s": summary["peak_oldest_pending_s"],
        "postgres_docker_cpu_pct": summary.get("postgres_docker_cpu_pct"),
        "slowest20_normal": {
            "n_breakdown_rows": len(rows),
            "n_unmatched_trigger": unmatched,
            "median_ms": {stage: median(r[stage] for r in slow) for stage in stages} if slow else None,
            "rows": slow,
        },
    }


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1])), indent=2))
