"""Build summary.json and summary.md for the p99 delivery-batch trials from each run's analysis.json and host.json.

Usage: python summarize.py <evidence folder> <label>... (labels in run order)
"""

import datetime
import glob
import json
import sys
from pathlib import Path

SERVICES = sorted(("postgres", "rabbitmq", "mailpit", "fake-telecom", "mock-ops", "api", "ingest-amqp", "ingest-logtail",
                   "rule-worker", "rule-worker-critical", "delivery-worker", "scheduler", "frontend", "app"),
                  key=len, reverse=True)
NORMAL_TARGET_MS = 5000
CRITICAL_TARGET_MS = 1000


def local(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def projects(containers):
    """Compose project names (from <project>-<service>-<n>) and other container names running at a snapshot."""
    found = {}
    for row in containers:
        name = row["Names"]
        project = next((name[: -len(f"-{svc}-{name.rsplit('-', 1)[-1]}")] for svc in SERVICES
                        if name.rsplit("-", 1)[-1].isdigit() and name.rsplit("-", 1)[0].endswith("-" + svc)), None)
        key = project or ("<other>" if name.startswith("supabase_") else name)
        found[key] = found.get(key, 0) + 1
    return found


def run_row(folder, label):
    analysis = json.loads((folder / "analysis.json").read_text())
    host = json.loads((folder / "host.json").read_text())
    summary = json.loads(Path(glob.glob(str(folder / "m11-*" / "summary.json"))[0]).read_text())
    start, end = summary["started_at_ms"], summary["sampled_until_ms"]
    t0, t1 = local(start - 30_000), local(end)
    window = [s for s in host["during_5s_samples"] if t0 <= s["local"] <= t1]
    foreign = sorted({p for s in window for p in s["test_processes"]})
    loads = [float(s["loadavg"].strip("{} ").split()[0]) for s in window]
    lat = analysis["receiver_latency"]
    rec = analysis["receipts"]
    ev = analysis["events"]
    env = host["delivery_worker_env_observed"] or []
    gates = {
        "normal_p99_lt_5s": lat["normal"]["p99_ms"] < NORMAL_TARGET_MS,
        "critical_p99_lt_1s": lat["critical"]["p99_ms"] < CRITICAL_TARGET_MS,
        "normal_2000_of_2000": rec["normal"] == {"received": 2000, "expected": 2000},
        "critical_240_of_240": rec["critical"] == {"received": 240, "expected": 240},
        "zero_duplicate_receipts": analysis["duplicate_receipts"] == 0 and analysis["invalid_receipts"] == 0,
        "no_accepted_event_loss": (ev["broker_confirmed"] == ev["accepted_after_drain"] == ev["processed_after_drain"]
                                   and ev["confirmed_not_stored_at_snapshot"] == 0),
    }
    return {
        "label": label,
        "run_id": analysis["run_id"],
        "requested": host["requested"],
        "delivery_worker_env_observed": env,
        "workload_sha256": analysis["workload_sha256"],
        "workers": analysis["workers"],
        "offer_local": f"{local(start)}–{local(summary['offer_ended_at_ms'])}",
        "trial_status": analysis["trial_status"],
        "receiver_latency": lat,
        "receipts": rec,
        "duplicate_receipts": analysis["duplicate_receipts"],
        "invalid_receipts": analysis["invalid_receipts"],
        "events": ev,
        "peak_delivery_backlog": analysis["peak_delivery_backlog"],
        "postgres_docker_cpu_pct": analysis["postgres_docker_cpu_pct"],
        "slowest20_normal_median_ms": analysis["slowest20_normal"]["median_ms"],
        "host": {
            "pre_loadavg": host["pre"]["loadavg"],
            "post_loadavg": host["post"]["loadavg"],
            "trial_window_load1_mean": round(sum(loads) / len(loads), 2) if loads else None,
            "trial_window_load1_max": max(loads) if loads else None,
            "foreign_test_or_build_processes_in_window": foreign,
            "containers_pre": len(host["pre"]["containers"]) or None,
            "compose_projects_pre": (projects(host["pre"]["containers"]) if host["pre"]["containers"]
                                     else host["pre"].get("compose_projects")),
            "foreign_processes_sampled": host.get("foreign_processes_sampled_during_run", True),
            "reconstructed_host_record": host.get("reconstructed", False),
        },
        "gates": gates,
    }


def fmt_lat(x):
    return f"{x['p50_ms']} / {x['p95_ms']} / {x['p99_ms']} (N={x['n']})"


def main(base, labels):
    rows = [run_row(base / label, label) for label in labels if (base / label / "analysis.json").exists()]
    (base / "summary.json").write_text(json.dumps({"runs": rows}, indent=2) + "\n")
    lines = ["| Run | Batch/Conc | Offer (local) | Normal p50/p95/p99 ms (N) | Critical p50/p95/p99 ms (N) "
             "| Receipts N / C | Dup | Lost events | Slowest-20 median: offer→ingest / ingest→proc / proc→claim / claim→receipt ms "
             "| Load1 mean/max in window | Foreign tests/builds in window |",
             "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- |"]
    for r in rows:
        m = r["slowest20_normal_median_ms"]
        ev = r["events"]
        lost = ev["broker_confirmed"] - ev["processed_after_drain"]
        lines.append(
            f"| {r['label']} | {r['requested']['DELIVERY_BATCH']}/{r['requested']['DELIVERY_CONCURRENCY']} "
            f"| {r['offer_local']} | {fmt_lat(r['receiver_latency']['normal'])} | {fmt_lat(r['receiver_latency']['critical'])} "
            f"| {r['receipts']['normal']['received']}/{r['receipts']['normal']['expected']} · "
            f"{r['receipts']['critical']['received']}/{r['receipts']['critical']['expected']} "
            f"| {r['duplicate_receipts']} | {lost} "
            f"| {m['offer_to_ingest_ms']:.0f} / {m['ingest_to_processed_ms']:.0f} / {m['processed_to_first_claim_ms']:.0f} / "
            f"{m['claim_to_receipt_ms']:.0f} "
            f"| {r['host']['trial_window_load1_mean']} / {r['host']['trial_window_load1_max']} "
            f"| {len(r['host']['foreign_test_or_build_processes_in_window']) if r['host']['foreign_processes_sampled'] else 'not sampled'} |")
    (base / "summary-table.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2:])
