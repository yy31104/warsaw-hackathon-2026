"""Verify saved M11 measurements and build an English report, figures, and a standalone web page."""

import argparse
import base64
import csv
import gzip
import hashlib
import json
import math
import os
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator
from PIL import Image, ImageDraw, ImageFont


WORKERS = (1, 2, 4)
KEYS = (1, 1000)
RATES = (700, 1400, 2800, 5600)
COLORS = {1: "#15697e", 2: "#a7552b", 4: "#285ca4"}
INK = "#152832"
MUTED = "#4c6069"
GRID = "#dce6e9"
TAB = {1: "One hot key", 1000: "1,000 normal keys"}


def read(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def percentile(values, p):
    values = sorted(values)
    return values[math.ceil(p * len(values) / 100) - 1] if values else None


def verify_run(folder, summary):
    run_id = summary["run_id"]
    start, end = summary["started_at_ms"], summary["offer_ended_at_ms"]
    with (folder / "observations.json.gz").open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    with gzip.open(folder / "observations.json.gz", "rt", encoding="utf-8") as file:
        raw = json.load(file)
    expected_hash = (folder / "observations.json.gz.sha256").read_text(encoding="ascii").split()[0]
    sent = raw["sent"]
    events = raw["events"]
    statuses = Counter(row["status"] for row in sent)
    raw_ids = {row["source_event_id"] for row in events}
    sent_ids = {row["event_id"] for row in sent}
    processed = [row["processed_at_ms"] for row in events if row["processed_at_ms"] is not None]
    steady_start = start + summary["backlog"]["warmup_s"] * 1000
    replicas = Counter(item["Service"] for item in read(folder / "compose-ps.json") if item["State"] == "running")
    checks = {
        "sha256": digest == expected_hash,
        "matching_run_id": raw["run_id"] == run_id and all(
            (folder / name).read_text(encoding="utf-8").splitlines()[0] == f"RUN ID {run_id}"
            for name in ("benchmark-terminal.txt", "compose-ps.txt")
        ),
        "compose_replicas": replicas["rule-worker"] == summary["normal_workers"]
        and replicas["ingest-amqp"] == summary["ingest_workers"]
        and replicas["rule-worker-critical"] == 1
        and replicas["delivery-worker"] == summary.get("actual_delivery_workers", 1),
        "unique_inputs_and_broker_totals": len(sent_ids) == len(sent) == summary["scheduled"]
        and statuses["confirmed"] == summary["broker_confirmed"]
        and statuses["dropped"] == summary["generator_dropped"]
        and statuses["error"] == summary["publish_errors"]
        and sum(start <= row.get("broker_confirmed_at_ms", 0) < end for row in sent)
        == summary["broker_confirmed_in_window"],
        "committed_inputs": len(raw_ids) == len(events) == summary["accepted_after_drain"]
        and raw_ids <= sent_ids and raw["rejections_by_kind"].get("REJECTED", 0) == summary["rejected"]
        and sum(start <= row["received_at_ms"] < end for row in events) == summary["accepted_in_window"],
        "processed_windows": len(processed) == summary["processed_after_drain"]
        and sum(start <= timestamp < end for timestamp in processed) == summary["processed_in_window"]
        and math.isclose(sum(steady_start <= timestamp < end for timestamp in processed)
                         / ((end - steady_start) / 1000), summary["processed_steady_per_s"]),
    }
    samples = raw["samples"]
    checks["published_backlog_samples"] = samples == read(folder / "samples.json")
    stage_series = {
        "pipeline": [(r["observed_at_ms"], r["rabbit_ready"] + r["processing_backlog"]) for r in samples],
        **{stage: [(r["observed_at_ms"], r[stage]) for r in samples]
           for stage in ("rabbit_ready", "processing_backlog", "delivery_backlog")},
    }
    checks["backlog_medians_and_trends"] = compare_backlogs(stage_series, summary, steady_start, end)
    outbox = {item["id"]: item for item in raw["outbox"]}
    receivers = {item["idempotency_key"]: item for item in raw["receiver_receipts"]}
    delays = defaultdict(list)
    for uuid, receipt in receivers.items():
        planned = outbox.get(uuid)
        if not planned or planned["gen_sent_at_ms"] is None:
            continue
        if (receipt["channel"], receipt["to"], receipt["gen_sent_at"]) != (
            planned["channel"], planned["contact"], planned["gen_sent_at_ms"]
        ):
            continue
        delay = receipt["received_at"] - receipt["gen_sent_at"]
        if delay >= 0:
            delays[planned["lane"]].append(delay)
    checks["receiver_percentiles"] = all(
        summary["receiver_latency"][lane] == {
            "n": len(delays[lane]),
            **{f"p{p}_ms": percentile(delays[lane], p) for p in (50, 95, 99)},
        }
        for lane in ("normal", "critical")
    )
    services = {item["Name"]: item["Service"] for item in read(folder / "compose-ps.json")}
    cpu = defaultdict(list)
    with (folder / "docker-stats.jsonl").open(encoding="utf-8") as file:
        for line in file:
            sample = json.loads(line)
            if not start <= sample["observed_at_ms"] < end:
                continue
            totals = defaultdict(float)
            for item in sample.get("containers", []):
                service = services.get(item["Name"])
                if service:
                    totals[service] += float(item["CPUPerc"].rstrip("%"))
            for service, usage in totals.items():
                cpu[service].append(usage)
    postgres = cpu["postgres"]
    reported_cpu = summary["postgres_docker_cpu_pct"]
    checks["postgres_cpu_samples"] = (
        bool(postgres) and len(postgres) == reported_cpu["n"]
        and math.isclose(sum(postgres) / len(postgres), reported_cpu["mean"])
        and math.isclose(max(postgres), reported_cpu["peak"])
    )
    checks["postgres_sampled_waits"] = summary["postgres_active_wait_session_samples"] == lock_totals(
        raw["samples"], start, end
    )
    broker_by_lane = defaultdict(Counter)
    for row in sent:
        lane = broker_by_lane[row["lane"]]
        lane["scheduled"] += 1
        lane[row["status"]] += 1
        lane["confirmed_in_window"] += int(start <= row.get("broker_confirmed_at_ms", 0) < end)
    return {
        "run_id": run_id, "checks": checks, "status": "PASS" if all(checks.values()) else "FAIL",
        "publisher_by_lane": broker_by_lane,
        "service_cpu_mean_percent": {name: round(sum(values) / len(values), 2)
                                     for name, values in cpu.items() if values},
    }


def compare_backlogs(stage_series, summary, start, end):
    boundaries = [start + (end - start) * position / 3 for position in range(4)]
    threshold = max(20, (summary["normal_offer_per_s"] + summary["critical_offer_per_s"]) / 4)
    for stage, series in stage_series.items():
        parts = [[value for ts, value in series if left <= ts < right]
                 for left, right in zip(boundaries, boundaries[1:])]
        medians = [median(part) if part else None for part in parts]
        expected = summary["backlog"][stage]
        if medians != expected["block_medians"]:
            return False
        if None in medians:
            trend = "insufficient samples"
        elif min(medians[1] - medians[0], medians[2] - medians[1]) > threshold:
            trend = "growing"
        else:
            trend = "no sustained growth"
        if trend != expected["trend"]:
            return False
    return True


def lock_totals(samples, start, end):
    totals = Counter()
    for row in samples:
        if start <= row["observed_at_ms"] < end:
            for wait in row["pg_activity"]:
                totals[f"{wait['type']}:{wait['event']}"] += wait["sessions"]
    return dict(totals)


def find_knees(runs):
    found = {str(keys): {} for keys in KEYS}
    for keys in KEYS:
        for workers in WORKERS:
            values = [r["normal_offer_per_s"] for r in runs
                      if (r["normal_keys"], r["normal_workers"]) == (keys, workers)
                      and r["backlog"]["pipeline"]["trend"] == "growing"]
            found[str(keys)][str(workers)] = min(values, default=None)
    return found


def configure_plot():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "axes.labelcolor": INK, "axes.edgecolor": GRID,
        "axes.titlesize": 12, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "figure.facecolor": "white", "axes.facecolor": "white", "svg.fonttype": "none",
    })


def plot_throughput(runs, path):
    fig, axes = plt.subplots(1, 2, figsize=(11.6, 4.25), sharey=True)
    for ax, keys in zip(axes, KEYS):
        ax.plot((0, 5700), (2, 5702), "--", color="#a0adb1", linewidth=1.1, label="Keeps pace")
        for workers in WORKERS:
            regime = {r["normal_offer_per_s"]: r for r in runs
                      if (r["normal_keys"], r["normal_workers"]) == (keys, workers)}
            label = f"{workers} worker" + ("s" if workers != 1 else "")
            ax.plot(RATES, [regime[r]["processed_steady_per_s"] if r in regime else math.nan for r in RATES],
                    color=COLORS[workers], marker="o", markersize=4, linewidth=1.9, label=label)
            for rate, row in regime.items():
                if row["backlog"]["pipeline"]["trend"] == "growing":
                    ax.scatter(rate, row["processed_steady_per_s"], s=77, facecolors="white",
                               edgecolors=COLORS[workers], linewidths=1.5, zorder=4)
        ax.set_title(TAB[keys], loc="left", fontweight="bold")
        ax.set_xticks(RATES, labels=[f"{rate:,}" for rate in RATES])
        ax.set(xlim=(395, 5880), ylim=(0, 6050), xlabel="Target normal offer, events/s")
        ax.yaxis.set_major_locator(MaxNLocator(7))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda n, _: f"{n:,.0f}"))
        ax.grid(axis="y", color=GRID, linewidth=.75)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
    axes[0].set_ylabel("Sustained processed events per second")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles[1:] + handles[:1], labels[1:] + labels[:1], ncols=4,
               loc="upper center", bbox_to_anchor=(0.5, .99), frameon=False, fontsize=9)
    fig.subplots_adjust(left=.085, right=.985, bottom=.15, top=.8, wspace=.135)
    fig.savefig(path, dpi=185, bbox_inches="tight", pad_inches=.18)
    plt.close(fig)


def plot_backlog(folder, run, path):
    rows = read(folder / "samples.json")
    start = run["started_at_ms"]
    elapsed = [(r["observed_at_ms"] - start) / 1000 for r in rows]
    fig, ax = plt.subplots(figsize=(11.2, 4.0))
    for key, label, color in (
        ("rabbit_ready", "RabbitMQ ready", "#285ca4"),
        ("processing_backlog", "PostgreSQL pending inbox", "#15697e"),
        ("delivery_backlog", "Pending delivery", "#a7552b"),
    ):
        ax.plot(elapsed, [r[key] for r in rows], linewidth=1.75, color=color, label=label)
    ax.axvspan(0, run["duration_s"], color="#eaf4f1", zorder=-2)
    ax.axvline(run["duration_s"], color="#85979b", linestyle="--", linewidth=1.2)
    ax.set(xlim=(0, max(elapsed)), ylim=(0, None), xlabel="Elapsed seconds", ylabel="Pending items")
    ax.set_title(f"Stage backlogs · {run['run_id']}", loc="left", fontweight="bold")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda n, _: f"{n:,.0f}"))
    ax.grid(axis="y", color=GRID, linewidth=.8)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=185, bbox_inches="tight", pad_inches=.18)
    plt.close(fig)


def font(name, size):
    path = Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / name
    fallback = "DejaVuSansMono.ttf" if name.startswith("con") else "DejaVuSans.ttf"
    return ImageFont.truetype(str(path) if path.is_file() else fallback, size)


def terminal_image(source, target, title):
    lines = source.read_text(encoding="utf-8").splitlines()
    run = lines[0].removeprefix("RUN ID ")
    if "高盛" in "".join(lines):
        # Console captures can contain CJK characters in mounted paths.
        pc_font = Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    else:
        pc_font = None
    console = font("consola.ttf", 20)
    cjk = ImageFont.truetype(str(pc_font), 18) if pc_font and pc_font.is_file() else console
    width, gap = 1540, 29
    wrapper = textwrap.TextWrapper(width=121, break_long_words=True, break_on_hyphens=False,
                                   replace_whitespace=False, drop_whitespace=True,
                                   subsequent_indent="    ")
    wrapped = []
    for line in lines[1:]:
        for item in wrapper.wrap(line) or [""]:
            wrapped.append((item, line.startswith(">"), line.startswith("RUN ID")))
    image = Image.new("RGB", (width, 171 + gap * len(wrapped)), "#10212d")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 134), fill="#153442")
    draw.text((38, 21), title, font=font("segoeuib.ttf", 27), fill="#f3f8fa")
    draw.text((39, 67), f"RUN ID {run}", font=font("consolab.ttf", 23), fill="#a0ead6")
    draw.text((39, 107), "Recorded command and terminal output", font=font("segoeui.ttf", 16), fill="#c6d9df")
    for i, (line, command, status) in enumerate(wrapped):
        color = "#8cdbed" if command else "#a0ead6" if status else "#e1ecf1"
        y = 149 + gap * i
        x = 39
        chunks = []
        for char in line:
            selected = cjk if ord(char) > 1024 else console
            if chunks and chunks[-1][0] is selected:
                chunks[-1][1] += char
            else:
                chunks.append([selected, char])
        for typeface, part in chunks:
            draw.text((x, y), part, font=typeface, fill=color)
            x += draw.textlength(part, font=typeface)
    image.save(target, optimize=True)


def display(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}".removesuffix(".0")


def lock_count(run):
    return sum(value for name, value in run.get("postgres_active_wait_session_samples", {}).items()
               if name.startswith("Lock:"))


def run_row(run):
    normal = run.get("receiver_latency", {}).get("normal", {})
    critical = run.get("receiver_latency", {}).get("critical", {})
    pipe = run.get("backlog", {})
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "ingest_consumers": run.get("actual_ingest_workers"),
        "delivery_workers": run.get("actual_delivery_workers", 1) if run.get("duration_s") else None,
        "normal_workers": run["normal_workers"],
        "normal_keys": run["normal_keys"],
        "normal_offered_per_s": run["normal_offer_per_s"],
        "critical_offered_per_s": run.get("critical_offer_per_s"),
        "duration_s": run.get("duration_s"),
        "scheduled": run.get("scheduled"),
        "broker_confirmed": run.get("broker_confirmed"),
        "broker_confirmed_in_window": run.get("broker_confirmed_in_window"),
        "broker_confirmed_per_s": run["broker_confirmed_in_window"] / run["duration_s"]
        if run.get("duration_s") else None,
        "p99_offer_lag_ms": run.get("p99_abs_offer_lag_ms"),
        "ingested_per_s": run.get("accepted_in_window_per_s"),
        "processed_per_s": run.get("processed_in_window_per_s"),
        "sustained_processed_per_s": run.get("processed_steady_per_s"),
        "generator_dropped": run.get("generator_dropped"),
        "publish_errors": run.get("publish_errors"),
        "rejected": run.get("rejected"),
        "committed_at_snapshot": run.get("accepted_after_drain"),
        "processed_at_snapshot": run.get("processed_after_drain"),
        "confirmed_not_stored_at_snapshot": run.get("confirmed_not_stored_at_snapshot"),
        "expected_receipts": run.get("notifications_expected"),
        "verified_receipts": run.get("notifications_verified"),
        "peak_pipeline_backlog": run.get("peak_pipeline_backlog"),
        "pipeline_trend": pipe.get("pipeline", {}).get("trend"),
        "rabbit_ready_trend": pipe.get("rabbit_ready", {}).get("trend"),
        "db_pending_trend": pipe.get("processing_backlog", {}).get("trend"),
        "delivery_pending_trend": pipe.get("delivery_backlog", {}).get("trend"),
        "postgres_cpu_mean_pct": run.get("postgres_docker_cpu_pct", {}).get("mean"),
        "postgres_cpu_peak_pct": run.get("postgres_docker_cpu_pct", {}).get("peak"),
        "postgres_cpu_sample_n": run.get("postgres_docker_cpu_pct", {}).get("n"),
        "sampled_lock_waiting_sessions": lock_count(run) if run.get("duration_s") else None,
        "sampled_lwlock_waiting_sessions": sum(
            count for reason, count in run.get("postgres_active_wait_session_samples", {}).items()
            if reason.startswith("LWLock:")
        ) if run.get("duration_s") else None,
        **{f"normal_{key}": normal.get(key) for key in ("n", "p50_ms", "p95_ms", "p99_ms")},
        **{f"critical_{key}": critical.get(key) for key in ("n", "p50_ms", "p95_ms", "p99_ms")},
        "workload_sha256": run.get("workload_sha256"),
    }


def markdown_latency(run, lane):
    value = run["receiver_latency"][lane]
    return " / ".join(display(value[f"p{p}_ms"], 0) for p in (50, 95, 99)) + f" (N={value['n']:,})"


def report(output, batch, machine, runs, knees, representative, audits, diagnostics, note):
    first = representative
    statuses = Counter(r["status"] for r in runs)
    remaining = max(0, batch["planned_runs"] - len(runs))
    verified = [r for r in runs if r["status"] == "PASS"]
    total = sum(r["scheduled"] for r in verified)
    confirms = sum(r["broker_confirmed"] for r in verified)
    drops = sum(r["generator_dropped"] for r in verified)
    errors = sum(r["publish_errors"] for r in verified)
    rejects = sum(r["rejected"] for r in verified)
    p = max(verified, key=lambda r: r["processed_steady_per_s"])
    matched = all(len({r["workload_sha256"] for r in verified
                       if r["normal_keys"] == keys and r["normal_offer_per_s"] == rate}) <= 1
                  for keys in KEYS for rate in RATES)
    spec = machine["docker"]
    root = [
        "# Alert Funnel saturation benchmark",
        "",
        f"Batch **{batch['batch_id']}** · source **{machine['git_commit'][:12]}** · "
        f"{statuses['PASS']} PASS, {statuses['FAIL']} FAIL, {remaining} NOT_RUN.",
        "Run status describes measurement and evidence collection. Capacity and delivery outcomes are shown below.",
        "",
        f"The verified runs scheduled **{total:,} synthetic events** across RabbitMQ. "
        f"The highest measured sustained processing rate was **{display(p['processed_steady_per_s'])} events/s** "
        f"with {p['normal_workers']} normal workers and {p['normal_keys']:,} normal alert keys. "
        "The throughput and latency figures come from the running stack and simulated receivers.",
        "",
        f"RabbitMQ confirmed {confirms:,} events. The generator dropped {drops:,}, the publisher reported "
        f"{errors:,} errors, and ingestion rejected {rejects:,} events. Each run uses a bounded observation "
        "window. A broker-confirmed event still waiting at the final snapshot is tracked as pending.",
        "",
        "## Throughput and the saturation knee",
        "",
        "![Two measured throughput curves by key shape and worker count](throughput.png)",
        "",
        "| Normal workers | First rising backlog, one hot key | First rising backlog, 1,000 keys |",
        "|---:|---:|---:|",
    ]
    for w in WORKERS:
        cells = [f"{knees[str(keys)][str(w)]:,}/s" if knees[str(keys)][str(w)] is not None
                 else "Outside observed rates" for keys in KEYS]
        root.append(f"| {w} | {' | '.join(cells)} |")
    root += [
        "",
        "A knee is the first tested normal offer where pipeline backlog grows by more than a quarter "
        "second of scheduled traffic between both adjacent block medians, with a minimum threshold of 20 items. "
        "The three blocks cover the offer window after warm-up. Pipeline backlog adds RabbitMQ messages ready "
        "for consumption to pending PostgreSQL inbox rows. RabbitMQ deliveries already assigned to a consumer "
        "sit outside this count.",
        "",
        "## Bottleneck evidence",
        "",
    ]
    if note:
        root += [note.strip(), ""]
    root += [
        f"Run **{first['run_id']}** observed {display(first['processed_steady_per_s'])} sustained events/s, "
        f"a peak pipeline backlog of {first['peak_pipeline_backlog']:,}, "
        f"and {first['notifications_verified']:,} verified receipts. "
        "The separate stage curves locate where the work accumulated.",
        "",
        "![Measured broker, inbox and delivery backlog over time](backlog.png)",
        "",
        "| Worker, key shape and offer | RabbitMQ ready medians | PostgreSQL pending medians | Delivery pending medians | PostgreSQL CPU mean / peak | Lock-wait session samples |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for keys in KEYS:
        for w in WORKERS:
            target = knees[str(keys)][str(w)]
            same = [r for r in verified if (r["normal_keys"], r["normal_workers"]) == (keys, w)]
            if not same:
                continue
            row = next((r for r in same if r["normal_offer_per_s"] == target),
                       max(same, key=lambda r: r["normal_offer_per_s"]))
            medians = [" / ".join(display(n, 0) for n in row["backlog"][field]["block_medians"])
                       for field in ("rabbit_ready", "processing_backlog", "delivery_backlog")]
            cpu = row["postgres_docker_cpu_pct"]
            root.append(f"| {w} · {TAB[keys]} · {row['normal_offer_per_s']:,}/s | "
                        + " | ".join(medians)
                        + f" | {display(cpu['mean'])}% / {display(cpu['peak'])}% | {lock_count(row):,} |")
    if diagnostics:
        root += [
            "", "### Controlled diagnostic repeats", "",
            "| Run ID | Ingest / rule / delivery workers | Keys | Offered/s | Ingested/s | Sustained processed/s | Broker confirms in window | Peak pipeline / delivery backlog | Normal p99 | Verdict |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for folder, row in diagnostics:
            relative = (folder / row["run_id"] / "summary.json").relative_to(output).as_posix()
            root.append(f"| [{row['run_id']}]({relative}) | {row['actual_ingest_workers']} / "
                        f"{row['actual_normal_workers']} / {row.get('actual_delivery_workers', 1)} | "
                        f"{row['normal_keys']:,} | "
                        f"{row['normal_offer_per_s']:,} + {row['critical_offer_per_s']} | "
                        f"{display(row['accepted_in_window_per_s'])} | {display(row['processed_steady_per_s'])} | "
                        f"{row['broker_confirmed_in_window']:,} | {row['peak_pipeline_backlog']:,} / "
                        f"{row['peak_delivery_backlog']:,} | {display(row['receiver_latency']['normal']['p99_ms'], 0)} ms "
                        f"| {row['status']} |")
    root += [
        "", "## Measured runs", "",
        "The processing column below covers only the 60-second offer. The figure uses the sustained "
        "rate after warm-up. The latency columns show p50 / p95 / p99 in milliseconds and receipt counts.",
        "",
        "| Normal keys | Workers | Target normal/s | Broker confirms/s | Ingested/s | Processed/s | Normal latency (ms) | Critical latency (ms) | Peak pending | Dropped / errors / rejected | Trend |",
        "|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---|",
    ]
    for r in runs:
        if r["status"] != "PASS":
            root.append(f"| {r['normal_keys']} | {r['normal_workers']} | {r['normal_offer_per_s']} "
                        f"| — | — | — | — | — | — | — | {r['status']} |")
            continue
        root.append(f"| {r['normal_keys']:,} | {r['normal_workers']} | {r['normal_offer_per_s']:,} | "
                    f"{display(r['broker_confirmed_in_window'] / r['duration_s'])} | "
                    f"{display(r['accepted_in_window_per_s'])} | {display(r['processed_in_window_per_s'])} | "
                    f"{markdown_latency(r, 'normal')} | {markdown_latency(r, 'critical')} | "
                    f"{r['peak_pipeline_backlog']:,} | {r['generator_dropped']:,} / "
                    f"{r['publish_errors']:,} / {r['rejected']:,} | {r['backlog']['pipeline']['trend']} |")
    run_id = first["run_id"]
    root += [
        "",
        "Short runs with one hot key produce few ordinary receipts because the rule groups them into "
        "one alert. Its percentiles therefore describe those individual receipts. A receipt still pending "
        "when the bounded observation ended is shown as unverified, with no delay estimated.",
        "",
        "## Terminal evidence",
        "",
        f"Both images below use run ID **{run_id}**. They render the captured terminal text. "
        f"The unabridged [Compose output]({run_id}/compose-ps.txt) and "
        f"[benchmark output]({run_id}/benchmark-terminal.txt) are separate files.",
        "",
        "![Compose ps, including the running normal, critical and delivery workers](compose-ps.png)",
        "",
        "![Benchmark terminal, with the same run ID and measured counts](benchmark-terminal.png)",
        "",
        "## Method and reproduction",
        "",
        f"- {machine['host_cpu']} · {spec['NCPU']} Docker vCPUs · "
        f"{spec['MemTotal'] / (1024 ** 3):.2f} GiB Docker memory. "
        f"Docker {spec['ServerVersion']}. [Machine and image IDs](machine.json)",
        "- Fresh Compose volumes for each run. One critical rule worker, one delivery worker and one "
        "RabbitMQ intake consumer in the main matrix. Each proposed normal-worker count was verified in Compose.",
        "- The workload generator assigns stable message IDs and group keys to the fixture. The same "
        f"workload hashes across normal-worker counts were {'verified' if matched else 'unverified'}.",
        "- Measure broker confirms and committed events separately. The offer lasts 60 seconds. The "
        f"observer allows up to {machine['settings']['drain_seconds']} more seconds to capture completions. "
        "Ingest rates use the source receipt timestamp of committed rows. Processing rates use the "
        "rule worker's recorded processing timestamp within the offer or steady window.",
        "- Match notification UUID, channel, recipient and generation timestamp against the phone "
        "simulator's receipt ledger. Latency runs from the actual offer timestamp to the receiver timestamp "
        "in the same Docker VM, including broker wait. This is measured in each lane independently.",
        "- PostgreSQL CPU is sampled from Docker stats. Roughly 100% is one logical core. Lock waits "
        "are counts of active waiting sessions in sampled observations from PostgreSQL, rather than "
        "cumulative lock-wait time.",
        f"- Recalculate receipt percentiles, event counts and evidence checksums separately. "
        f"**{sum(r['status'] == 'PASS' for r in audits)}/{len(audits)}** raw evidence checks passed. "
        "[Verification details](verification.json)",
        "",
        f"Check out `{machine['git_commit']}`, extract the [exact measurement harness](harness.zip) in the repository root, "
        "and run with Docker Desktop, Docker Compose and Python 3.12.",
        "",
        '```powershell',
        'docker compose pull postgres rabbitmq mailpit',
        'python loadtest/run_saturation.py --output loadtest/results/saturation-REPEAT',
        '```',
        "",
        "The [recorded command](reproduce.txt) and each run's `commands.json` preserve the invocation that "
        "produced this report. The repository evidence includes per-run summaries, time series, Docker CPU and "
        "memory samples, Compose status and terminal output. The representative run's compressed event and "
        f"receipt ledger is also available. [All run metrics](runs.csv) · [Representative raw ledger]({run_id}/observations.json.gz)",
    ]
    (output / "REPORT.md").write_text("\n".join(root) + "\n", encoding="utf-8")


def build_site(output, batch, machine, rows, knees, selected, finding):
    backlog = selected["backlog"]
    data = {
        "batchId": batch["batch_id"], "commit": machine["git_commit"][:12], "planned": batch["planned_runs"],
        "knees": knees, "selected": selected["run_id"],
        "finding": finding,
        "machine": f"{machine['host_cpu']} · {machine['docker']['NCPU']} Docker vCPUs",
        "stages": {
            "offer": selected["normal_offer_per_s"], "critical": selected["critical_offer_per_s"],
            "workers": selected["actual_normal_workers"], "keys": selected["normal_keys"],
            "rabbit": backlog["rabbit_ready"]["block_medians"][-1],
            "inbox": backlog["processing_backlog"]["block_medians"][-1],
            "delivery": backlog["delivery_backlog"]["block_medians"][-1],
        },
        "runs": [{
            "keys": row["normal_keys"], "workers": row["normal_workers"], "offer": row["normal_offered_per_s"],
            "ingested": row["ingested_per_s"], "steady": row["sustained_processed_per_s"],
            "normalP99": row["normal_p99_ms"], "normalN": row["normal_n"], "criticalP99": row["critical_p99_ms"],
            "criticalN": row["critical_n"], "cpu": row["postgres_cpu_mean_pct"],
            "trend": row["pipeline_trend"], "status": row["status"],
        } for row in rows],
    }
    template = Path(__file__).with_name("saturation_site.html").read_text(encoding="utf-8")
    for token, name in (("CSV", "runs.csv"), ("THROUGHPUT", "throughput.png"), ("BACKLOG", "backlog.png"),
                        ("COMPOSE", "compose-ps.png"), ("TERMINAL", "benchmark-terminal.png")):
        template = template.replace(f"@@{token}@@", base64.b64encode((output / name).read_bytes()).decode("ascii"))
    template = template.replace("@@DATA@@", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (output / "website.html").write_text(template, encoding="utf-8")


def main(output, diagnostic_paths, note_path):
    batch = read(output / "batch.json")
    machine = read(output / "machine.json")
    runs = sorted(batch["runs"], key=lambda r: (r["normal_keys"], r["normal_workers"], r["normal_offer_per_s"]))
    data = [run_row(run) for run in runs]
    with (output / "runs.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    verified = [r for r in runs if r["status"] == "PASS"]
    if not verified:
        raise SystemExit("No completed measurements to chart.")
    diagnostics = [(path, row) for path in diagnostic_paths for row in read(path / "batch.json")["runs"]
                   if row.get("duration_s")]
    audits = []
    for folder, row in [(output, r) for r in verified] + diagnostics:
        item = verify_run(folder / row["run_id"], row)
        audits.append(item)
        print(f"{row['run_id']} evidence {item['status']}", flush=True)
    (output / "verification.json").write_text(json.dumps(audits, indent=2) + "\n", encoding="utf-8")
    if any(item["status"] != "PASS" for item in audits):
        raise SystemExit("Evidence mismatch. See verification.json.")
    knees = find_knees(verified)
    preferred = [r for r in verified if (r["normal_workers"], r["normal_keys"]) == (4, 1000)] or verified
    selected = next((r for r in preferred if r["backlog"]["pipeline"]["trend"] == "growing"), preferred[-1])
    folder = output / selected["run_id"]
    configure_plot()
    plot_throughput(verified, output / "throughput.png")
    plot_backlog(folder, selected, output / "backlog.png")
    terminal_image(folder / "compose-ps.txt", output / "compose-ps.png", "Docker Compose service status")
    terminal_image(folder / "benchmark-terminal.txt", output / "benchmark-terminal.png", "Benchmark terminal")
    note = note_path.read_text(encoding="utf-8") if note_path else ""
    report(output, batch, machine, runs, knees, selected, audits, diagnostics, note)
    finding = ' '.join(note.strip().split('\n\n', 1)[0].split()) if note else ""
    build_site(output, batch, machine, data, knees, selected, finding)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--diagnostic", type=Path, action="append", default=[])
    parser.add_argument("--analysis-note", type=Path)
    args = parser.parse_args()
    main(args.output.resolve(), [p.resolve() for p in args.diagnostic], args.analysis_note)
