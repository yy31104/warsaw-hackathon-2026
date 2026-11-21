"""Run the M11 matrix sequentially with clean Docker volumes and record the actual commands."""

import argparse
import hashlib
import itertools
import json
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RATES = (700, 1400, 2800, 5600)
WORKERS = (1, 2, 4)
KEYS = (1, 1000)
BACKEND = ("api", "ingest-amqp", "rule-worker", "rule-worker-critical", "delivery-worker", "scheduler")
PORTS = ("POSTGRES_PORT", "RABBITMQ_PORT", "RABBITMQ_MANAGEMENT_PORT", "SMTP_PORT", "MAILPIT_PORT",
         "TELECOM_PORT", "MOCK_OPS_PORT", "API_PORT", "FRONTEND_PORT")
STATS_FORMAT = "{{json .}}"
PS_FORMAT = "table {{.Service}}\t{{.Name}}\t{{.State}}\t{{.Health}}"


def command(args, *, env=None, check=True, **kwargs):
    return subprocess.run(args, cwd=ROOT, env=env, check=check, encoding="utf-8", errors="replace", **kwargs)


def capture(args, *, env=None, timeout=30):
    result = command(args, env=env, timeout=timeout, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{signature(args)}: {result.stderr or result.stdout}")
    return result.stdout.strip()


def signature(args):
    words = [str(arg) for arg in args]
    return subprocess.list2cmdline(words) if os.name == "nt" else shlex.join(words)


def utc_now():
    return datetime.fromtimestamp(time.time(), timezone.utc)


def now_ms():
    return time.time_ns() // 1_000_000


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_lines(stdout):
    if not stdout:
        return []
    if stdout.lstrip().startswith("["):
        return json.loads(stdout)
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def docker_stats(ids, output, stop):
    with output.open("w", encoding="utf-8") as file:
        while not stop.is_set():
            sampled_at = now_ms()
            try:
                stats = read_lines(capture(["docker", "stats", "--no-stream", "--format", STATS_FORMAT, *ids],
                                           timeout=8))
                row = {"requested_at_ms": sampled_at, "observed_at_ms": now_ms(), "containers": stats}
            except (subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
                row = {"requested_at_ms": sampled_at, "observed_at_ms": now_ms(), "error": str(exc)}
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
            file.flush()
            stop.wait(3)


def machine(commit, image_project, config):
    info = json.loads(capture(["docker", "info", "--format", "{{json .}}"]))
    linux = ("ServerVersion", "OSVersion", "OperatingSystem", "Architecture", "KernelVersion", "NCPU",
             "MemTotal", "CgroupVersion", "Driver")
    host_cpu = platform.processor()
    if os.name == "nt":
        try:
            host_cpu = capture(["pwsh", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    names = {
        "backend": f"{image_project}-backend:local",
        "fake-telecom": f"{image_project}-fake-telecom",
        "postgres": "postgres:16-alpine",
        "rabbitmq": "rabbitmq:3.13-management-alpine",
        "mailpit": "axllent/mailpit:latest",
    }
    return {
        "captured_at_utc": utc_now().isoformat(),
        "git_commit": commit,
        "host_os": platform.platform(),
        "host_cpu": host_cpu,
        "host_logical_cpus": os.cpu_count(),
        "docker": {name: info.get(name) for name in linux},
        "compose_version": capture(["docker", "compose", "version"]),
        "images": {role: {"tag": tag, "id": capture(["docker", "image", "inspect", "--format", "{{.Id}}", tag])}
                   for role, tag in names.items()},
        "settings": config,
        "workload_fixture_sha256": hashlib.sha256(
            (ROOT / "fixtures/events/workflow_failed.json").read_bytes()
        ).hexdigest(),
        "measurement_scripts_sha256": {
            name: hashlib.sha256((ROOT / "loadtest" / name).read_bytes()).hexdigest()
            for name in ("run_saturation.py", "saturation_trial.py", "bench_generate.py", "bench_collect.py")
        },
    }


def run_trial(folder, run_id, workers, keys, rate, args, override, env):
    folder.mkdir()
    project = run_id
    compose = ["docker", "compose", "--ansi", "never", "-p", project, "-f", str(ROOT / "docker-compose.yml"),
               "-f", str(override)]
    up = [*compose, "up", "-d", "--no-build", "--wait", "--wait-timeout", "90",
          "--scale", f"rule-worker={workers}", "--scale", f"ingest-amqp={args.ingest_workers}",
          "--scale", f"delivery-worker={args.delivery_workers}", *BACKEND]
    ps = [*compose, "ps", "--format", PS_FORMAT]
    generate = [
        *compose, "run", "--rm", "-T", "--no-deps", "--name", f"{project}-generator",
        "--volume", f"{ROOT / 'loadtest'}:/app/loadtest:ro",
        "--volume", f"{ROOT / 'fixtures'}:/app/fixtures:ro",
        "--volume", f"{folder}:/bench-output", "rule-worker", "python", "-m", "loadtest.saturation_trial",
        "--output", "/bench-output", "--run-id", run_id, "--tag", f"m11-k{keys}-r{rate}",
        "--duration", str(args.duration), "--drain-seconds", str(args.drain_seconds),
        "--normal-workers", str(workers), "--normal-rate", str(rate), "--normal-keys", str(keys),
        "--ingest-workers", str(args.ingest_workers),
        "--critical-rate", str(args.critical_rate), "--max-in-flight", str(args.max_in_flight),
    ]
    down = [*compose, "down", "--volumes", "--timeout", "8"]
    write_json(folder / "commands.json", {"run_id": run_id, "project": project,
               "environment_overrides": {k: env[k] for k in (*PORTS, "ASSIST_ENABLED", "DEMO_MODE")},
               "up": up, "ps": ps, "measure": generate, "cleanup": down})
    if capture([*compose, "ps", "-aq"], env=env) or capture(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]
    ):
        raise RuntimeError(f"Compose project {project} already exists.")
    try:
        with (folder / "compose-startup.txt").open("w", encoding="utf-8") as logs:
            command(up, env=env, stdout=logs, stderr=subprocess.STDOUT, timeout=125)
        running = read_lines(capture([*compose, "ps", "--format", "json"], env=env))
        write_json(folder / "compose-ps.json", running)
        active = [row["Service"] for row in running if row["State"] == "running"]
        actual = active.count("rule-worker")
        (folder / "compose-ps.txt").write_text(
            f"RUN ID {run_id}\n{utc_now().isoformat()}\n> {signature(ps)}\n\n"
            + capture(ps, env=env) + "\n", encoding="utf-8"
        )
        expected = {"rule-worker": workers, "ingest-amqp": args.ingest_workers,
                    "rule-worker-critical": 1, "delivery-worker": args.delivery_workers}
        if any(active.count(service) != count for service, count in expected.items()):
            raise RuntimeError(f"Expected Compose worker counts {expected}, observed {active}.")
        with (folder / "benchmark-terminal.txt").open("w", encoding="utf-8") as terminal:
            terminal.write(f"RUN ID {run_id}\n{utc_now().isoformat()}\n> {signature(generate)}\n\n")
            terminal.flush()
            stop = threading.Event()
            watcher = threading.Thread(target=docker_stats,
                                       args=([row["ID"] for row in running if row["State"] == "running"],
                                             folder / "docker-stats.jsonl", stop))
            watcher.start()
            try:
                result = command(generate, env=env, stdout=terminal, stderr=subprocess.STDOUT, check=False,
                                 timeout=args.duration + args.drain_seconds + 120)
            finally:
                stop.set()
                watcher.join()
            terminal.write(f"\nExit code {result.returncode}\n")
        if result.returncode:
            raise RuntimeError(f"Measurement exited {result.returncode}. See benchmark-terminal.txt.")
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        start, end = summary["started_at_ms"], summary["offer_ended_at_ms"]
        rows = read_lines((folder / "docker-stats.jsonl").read_text(encoding="utf-8"))
        pg_samples = [(row["observed_at_ms"], float(stat["CPUPerc"].rstrip("%"))) for row in rows
                      if start <= row["observed_at_ms"] < end
                      for stat in row.get("containers", []) if stat["Name"].endswith("-postgres-1")]
        summary["postgres_docker_cpu_pct"] = {
            "n": len(pg_samples),
            "mean": sum(pct for _, pct in pg_samples) / len(pg_samples) if pg_samples else None,
            "peak": max((pct for _, pct in pg_samples), default=None),
        }
        summary["actual_normal_workers"] = actual
        summary["actual_ingest_workers"] = active.count("ingest-amqp")
        summary["actual_delivery_workers"] = active.count("delivery-worker")
        if args.duration >= 60 and len(pg_samples) < 3:
            summary["status"] = "FAIL"
            summary["measurement_errors"].append("Fewer than three PostgreSQL Docker CPU samples.")
        write_json(folder / "summary.json", summary)
        with (folder / "observations.json.gz").open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        (folder / "observations.json.gz.sha256").write_text(
            f"{digest}  observations.json.gz\n", encoding="ascii"
        )
        return summary
    finally:
        with (folder / "compose-cleanup.txt").open("w", encoding="utf-8") as logs:
            logs.write(f"RUN ID {run_id}\n> {signature(down)}\n")
            logs.flush()
            command(down, env=env, stdout=logs, stderr=subprocess.STDOUT, timeout=90)


def main(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    commit = capture(["git", "rev-parse", "HEAD"])
    batch = "m11-" + utc_now().strftime("%m%d%H%M") + "-" + uuid4().hex[:5]
    image_project = args.image_project or f"{batch}-images"
    env = {**os.environ, **dict.fromkeys(PORTS, "0"), "ASSIST_ENABLED": "0", "DEMO_MODE": "1"}
    override = output / "compose.measurement.json"
    services = {service: {"image": f"{image_project}-backend:local", "pull_policy": "never"}
                for service in BACKEND}
    services["fake-telecom"] = {"image": f"{image_project}-fake-telecom", "pull_policy": "never"}
    write_json(override, {"services": services})
    build = ["docker", "compose", "--ansi", "never", "-p", image_project, "build", "api", "fake-telecom"]
    if args.image_project is None:
        with (output / "image-build.txt").open("w", encoding="utf-8") as logs:
            command(build, env=env, stdout=logs, stderr=subprocess.STDOUT, timeout=300)
    config = vars(args).copy()
    config["output"] = str(output)
    config["image_project"] = image_project
    write_json(output / "machine.json", machine(commit, image_project, config))
    (output / "reproduce.txt").write_text(
        f"Batch {batch}\nCommit {commit}\nFrom the repository root\n"
        f"{signature(build)}\n{signature([sys.executable, *sys.argv])}\n"
        "Each run includes an exact commands.json and two untouched terminal traces.\n",
        encoding="utf-8",
    )
    targets = list(itertools.product(args.rates, args.keys, args.workers))
    results = []
    for index, (rate, keys, workers) in enumerate(targets, 1):
        run_id = f"{batch}-r{rate}-k{keys}-w{workers}"
        print(f"[{index}/{len(targets)}] {run_id} started {utc_now().isoformat()}", flush=True)
        folder = output / run_id
        try:
            summary = run_trial(folder, run_id, workers, keys, rate, args, override, env)
            print(f"[{index}/{len(targets)}] {run_id} {summary['status']} "
                  f"{summary['processed_in_window_per_s']:.1f} processed/s, "
                  f"backlog {summary['backlog']['pipeline']['trend']}", flush=True)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            summary = {"run_id": run_id, "status": "FAIL", "normal_workers": workers,
                       "normal_keys": keys, "normal_offer_per_s": rate,
                       "measurement_errors": [str(exc)]}
            print(f"[{index}/{len(targets)}] {run_id} FAIL {exc}", flush=True)
        results.append(summary)
        write_json(output / "batch.json", {"batch_id": batch, "commit": commit,
                   "planned_runs": len(targets), "completed_runs": len(results), "runs": results})
    return 0 if all(row["status"] == "PASS" for row in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rates", nargs="+", type=int, default=RATES)
    parser.add_argument("--workers", nargs="+", type=int, default=WORKERS)
    parser.add_argument("--keys", nargs="+", type=int, default=KEYS)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--drain-seconds", type=int, default=20)
    parser.add_argument("--critical-rate", type=int, default=2)
    parser.add_argument("--ingest-workers", type=int, default=1, help="Change only for a controlled diagnostic.")
    parser.add_argument("--delivery-workers", type=int, default=1, help="Change only for a controlled diagnostic.")
    parser.add_argument("--max-in-flight", type=int, default=2000)
    parser.add_argument("--image-project", help="Reuse images built from the same commit with a previous Compose project.")
    args = parser.parse_args()
    if min(*args.rates, *args.workers, *args.keys, args.ingest_workers,
           args.delivery_workers, args.duration, args.max_in_flight) < 1:
        parser.error("Rates, workers, keys, duration and in-flight limit must be positive.")
    if min(args.drain_seconds, args.critical_rate) < 0:
        parser.error("Drain time and critical rate must be nonnegative.")
    raise SystemExit(main(args))
