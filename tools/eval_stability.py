#!/usr/bin/env python3
"""Score ask-all and adaptive minimax against the same synthetic operator answers."""

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter_ns

import yaml


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "stability"
FINAL_SHA256 = "73d56cade6e8e227fe5c57d022c2b16db1f3c1f713cb5829b50921d332a4f113"
FIXED_AT = "2026-11-20T10:00:00+00:00"
SPLITS = {"dev": ("dev.yaml", 8), "held-out": ("held_out.yaml", 4)}
METHODS = ("ask_all", "adaptive")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_cases(split):
    filename, count = SPLITS[split]
    path = FIXTURES / filename
    if split == "held-out" and digest(path) != FINAL_SHA256:
        raise ValueError("Frozen held-out fixture SHA-256 changed.")
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    if len(cases) != count or len({case["id"] for case in cases}) != count:
        raise ValueError(f"Expected {count} distinct {split} cases")
    for case in cases:
        axes = case["hypotheses"]
        ids = [axis["id"] for axis in axes]
        if set(ids) != set(case["oracle"]) or len(ids) != len(set(ids)):
            raise ValueError(f"{case['id']}: oracle must answer every unknown exactly once")
        for axis in axes:
            options = axis["values"]
            if len(set(options)) != len(options) or len(options) < 2 or case["oracle"][axis["id"]] not in options:
                raise ValueError(f"{case['id']}: invalid options for {axis['id']}")
        if not case["samples"]:
            raise ValueError(f"{case['id']}: no labelled samples")
        for sample in case["samples"]:
            label = sample["expected"]
            if not isinstance(label["critical"], bool) or (label["critical"] and not label["routes"]):
                raise ValueError(f"{case['id']}: a critical label needs an expected delivery")
    return cases


def run_method(method, case, draft, snapshot, clock, stability):
    axes = []
    answers = {}
    for axis in case["hypotheses"]:
        values = axis["values"]
        answers[axis["id"]] = values.index(case["oracle"][axis["id"]])
        axes.append({
            **{k: v for k, v in axis.items() if k != "values"},
            "candidates": [{"value": v, "source": "declared"} for v in values],
        })
    samples = [{"event": s["event"], "source": case["source"], "transport": case.get("transport", "http")}
               for s in case["samples"]]
    labels = [s["expected"] for s in case["samples"]]
    asked = []
    result = {"status": "ERROR", "selected_outcome": None}
    started = perf_counter_ns()
    try:
        if method == "ask_all":
            asked.extend(answers)
            axes = [{**a, "candidates": [a["candidates"][answers[a["id"]]]]} for a in axes]
        report = stability(samples, draft, axes, snapshot, clock=clock)
        result.update({k: report[k] for k in ("status", "total_combinations", "evaluated_combinations", "warnings")})
        if report["complete"]:
            by_id = {group["id"]: group["outcome"] for group in report["outcome_groups"]}
            if method == "ask_all":
                if len(by_id) != 1:
                    raise ValueError("A fully interpreted case must produce one outcome group")
                selected = next(iter(by_id))
            else:
                node = report["question_tree"]
                while "question_id" in node:
                    key = node["question_id"]
                    if key not in answers or key in asked:
                        raise ValueError(f"Invalid adaptive question {key!r}")
                    asked.append(key)
                    branches = {b["option_index"]: b["next"] for b in node["branches"]}
                    node = branches[answers[key]]
                selected = node["outcome_group"]
            output = sorted(by_id[selected], key=lambda row: row["sample_index"])
            if [row["sample_index"] for row in output] != list(range(len(samples))):
                raise ValueError("Selected outcome must include every sample exactly once")
            for row in output:
                for required in ("deliveries", "alerts", "matched_rule_ids"):
                    if required not in row:
                        raise ValueError(f"Selected outcome missing {required}")
            result["selected_outcome"] = output
        elif report["status"] != "INCOMPLETE":
            raise ValueError("Incomplete analysis did not report INCOMPLETE")
    except Exception as exc:
        # Keep the failed case in all denominators and retain the diagnostic for reproduction.
        result.update(status="ERROR", selected_outcome=None, error=f"{type(exc).__name__}: {exc}")
    result["runtime_ms"] = (perf_counter_ns() - started) / 1_000_000
    result["asked"] = asked
    outcome = result["selected_outcome"]
    result["questions_for_mean"] = len(asked) if outcome is not None else len(axes)

    routing_correct = outcome is not None
    rules_correct = outcome is not None
    critical_miss = False
    for index, label in enumerate(labels):
        row = outcome[index] if outcome is not None else {}
        actual = {(d["recipient"], d["channel"]) for d in row.get("deliveries", [])}
        required = {tuple(pair) for pair in label["routes"]}
        routing_correct &= actual == required
        rules_correct &= set(row.get("matched_rule_ids", [])) == set(label["rules"])
        if label["critical"]:
            critical_alert = any(a["severity"] == "critical" for a in row.get("alerts", []))
            critical_miss |= not critical_alert or not required.issubset(actual)
    result.update(routing_correct=routing_correct, matched_rules_correct=rules_correct, critical_miss=critical_miss)
    return result


def aggregate(rows, method):
    results = [row[method] for row in rows]
    times = sorted(result["runtime_ms"] for result in results)
    return {
        "cases": len(results), "critical_cases": sum(row["critical"] for row in rows),
        "routing_correct": sum(x["routing_correct"] for x in results),
        "matched_rules_correct": sum(x["matched_rules_correct"] for x in results),
        "critical_misses": sum(x["critical_miss"] for x in results),
        "incomplete": sum(x["status"] == "INCOMPLETE" for x in results),
        "errors": sum(x["status"] == "ERROR" for x in results),
        "questions_asked": sum(len(x["asked"]) for x in results),
        "mean_questions": statistics.mean(x["questions_for_mean"] for x in results),
        "p50_ms": times[math.ceil(0.50 * len(times)) - 1],
        "p95_ms": times[math.ceil(0.95 * len(times)) - 1],
    }


def markdown(report):
    lines = ["# Decision stability evaluation", "", f"Source commit `{report['commit']}`.", "",
             "| Split | Method | Mean questions | Routing correct | Rule matches correct | Critical misses | INCOMPLETE | Errors | p50 ms | p95 ms |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for split, methods in report["summary"].items():
        for method, score in methods.items():
            n = score["cases"]
            lines.append(
                f"| {split} | {method} | {score['mean_questions']:.2f} | {score['routing_correct']}/{n} | "
                f"{score['matched_rules_correct']}/{n} | {score['critical_misses']}/{score['critical_cases']} | "
                f"{score['incomplete']} | {score['errors']} | {score['p50_ms']:.3f} | {score['p95_ms']:.3f} |"
            )
    if "held_out_targets" in report:
        lines.extend(["", "## Held-out targets", ""])
        for label, passed in report["held_out_targets"].items():
            lines.append(f"- {label} — {'PASS' if passed else 'FAIL'}")
    lines.extend(["", "## Per-case questions", "",
                  "| Split | Case | Ask-all | Adaptive asked | Adaptive charged | Adaptive status |",
                  "|---|---|---:|---:|---:|---|"])
    for case in report["cases"]:
        a = case["adaptive"]
        lines.append(f"| {case['split']} | {case['id']} | {len(case['ask_all']['asked'])} | "
                     f"{len(a['asked'])} | {a['questions_for_mean']} | {a['status']} |")
    lines.extend(["", *report["measurement"], ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("all", *SPLITS), default="all")
    parser.add_argument("--output-dir", type=Path, help="Defaults to loadtest/results/stability-<commit>.")
    args = parser.parse_args()
    # Use the candidate checkout even if a caller has another editable environment or config directory.
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ["CONFIG_DIR"] = str(ROOT / "config")
    from alerting.clock import FixedClock
    from alerting.config.loader import build_snapshot
    from alerting.rules.stability import stability

    commit = git("rev-parse", "HEAD")
    destination = args.output_dir or ROOT / "loadtest" / "results" / f"stability-{commit[:12]}"
    splits = list(SPLITS) if args.split == "all" else [args.split]
    snapshot = build_snapshot(0, False, [])
    schemas = {s["id"]: {k: v for k, v in s.items() if k not in {"version", "origin"}}
               for s in snapshot.schemas}
    clock = FixedClock(datetime.fromisoformat(FIXED_AT))
    report = {
        "commit": commit, "scorecard_commit": git("log", "-1", "--format=%H", "--", "docs/SCORECARD.md"),
        "working_tree_changes": git("status", "--porcelain", "--", "backend/alerting", "config", "docs/SCORECARD.md",
                                    "fixtures/stability", "tools/eval_stability.py").splitlines(),
        "clock": FIXED_AT, "fixture_sha256": {}, "cases": [], "summary": {},
        "measurement": [
            "Each case has one ground-truth interpretation. Ask-all reveals every answer and evaluates that single world. Adaptive follows only the oracle's branch of the engine's minimax tree.",
            "Routing requires exact immediate recipient/channel pairs on every sample in a case. Rule matches are scored separately. A labelled critical case misses if any protected sample lacks a critical alert or an expected delivery.",
            "Unresolved cases remain in the correctness and critical denominators. Their question charge is every unknown. The case table also shows the actual number asked.",
            "Each case runs ask-all first. Runtime is one local monotonic measurement per case and method, including analysis and oracle traversal, excluding setup, scoring and file I/O. The percentiles use nearest rank.",
        ],
    }
    for split in splits:
        cases = read_cases(split)
        report["fixture_sha256"][split] = digest(FIXTURES / SPLITS[split][0])
        rows = []
        for case in cases:
            expected = [s["expected"] for s in case["samples"]]
            row = {"id": case["id"], "split": split, "oracle": case["oracle"], "expected": expected,
                   "critical": any(s["critical"] for s in expected)}
            for method in METHODS:
                row[method] = run_method(method, case, schemas[case["schema"]], snapshot, clock, stability)
            rows.append(row)
        report["cases"].extend(rows)
        report["summary"][split] = {m: aggregate(rows, m) for m in METHODS}
    if "held-out" in report["summary"]:
        final = report["summary"]["held-out"]
        adaptive = final["adaptive"]
        report["held_out_targets"] = {
            "Lower mean question count than ask-all": adaptive["mean_questions"] < final["ask_all"]["mean_questions"],
            "All four cases routed correctly": adaptive["routing_correct"] == 4,
            "Zero critical misses": adaptive["critical_misses"] == 0,
        }
    destination.mkdir(parents=True, exist_ok=True)
    prefix = "" if args.split == "all" else args.split + "-"
    (destination / (prefix + "report.json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = markdown(report)
    (destination / (prefix + "summary.md")).write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Evidence in {destination}")
    return int(any(s["errors"] for methods in report["summary"].values() for s in methods.values())
               or not all(report.get("held_out_targets", {}).values()))


if __name__ == "__main__":
    raise SystemExit(main())
