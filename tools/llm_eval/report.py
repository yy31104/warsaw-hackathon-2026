"""Summarize product quality, model contributions and provider-billed attempts separately."""
import json
import math
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import fmean


def totals(scores):
    out = {}
    for score in scores:
        for k, v in score.items():
            if isinstance(v, list):
                dest = out.setdefault(k, [0, 0])
                dest[0] += v[0]
                dest[1] += v[1]
            else:
                out[k] = out.get(k, 0) + v
    return out


def latency(values):
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    return {"n": len(ordered), **{f"p{p}_ms": round(ordered[math.ceil(p * len(ordered) / 100) - 1], 3)
                                for p in (50, 95)}}


def summarize(rows):
    calls = [row["call"] for row in rows if row["call"]]
    costs = [Decimal(c["estimated_cost_usd"]) for c in calls if c["estimated_cost_usd"] is not None]
    inputs = [c["input_tokens"] for c in calls if c["input_tokens"] is not None]
    outputs = [c["output_tokens"] for c in calls if c["output_tokens"] is not None]
    return {
        "case_status": dict(Counter(r["status"] for r in rows)),
        "reasons": dict(Counter(r["reason"] for r in rows if r["reason"])),
        "attribution": dict(Counter(r["attribution"] for r in rows)),
        "product": totals(r["product"] for r in rows if r["product"] is not None),
        "provider_only": totals(r["provider_only"] for r in rows if r["provider_only"] is not None),
        "provider_scored_cases": dict(Counter(r["task"] for r in rows if r["provider_only"] is not None)),
        "provider_outcomes": dict(Counter(c["outcome"] for c in calls)),
        "served_models": dict(Counter(c["response_model"] for c in calls if c["response_model"])),
        "endpoint_latency": latency(r["endpoint_latency_ms"] for r in rows),
        "provider_latency": latency(c["latency_ms"] for c in calls),
        "calls": len(calls), "costed_calls": len(costs),
        "usage_basis": dict(Counter(c.get("cost_basis") or "unknown" for c in calls)),
        "reported_input_tokens": {"n": len(inputs), "total": sum(inputs), "mean": fmean(inputs) if inputs else None},
        "reported_output_tokens": {"n": len(outputs), "total": sum(outputs), "mean": fmean(outputs) if outputs else None},
        "estimated_total_usd_known_calls": str(sum(costs, Decimal(0))) if costs else None,
        "mean_estimated_usd_per_costed_call": str(sum(costs) / len(costs)) if costs else None,
    }


def choose(modes, candidates):
    # Freeze the ranking on development data. The final split only measures it.
    eligible = []
    for name in candidates:
        s = modes[name]
        if not s["calls"] or s["case_status"].get("NOT_RUN") or not all(
                s["provider_scored_cases"].get(t) for t in ("schema", "runbook")):
            return {"status": "NOT_RUN", "model": None, "reason": "both candidates need measured calls on both tasks"}
        m = s["product"]
        if (any(m.get(k, 0) for k in ("invented_parameters", "risky_without_approval", "missing_risky_steps",
                                      "unsafe_executable_steps")) or s["costed_calls"] != s["calls"]
                or set(s["provider_outcomes"]) != {"success"} or len(s["served_models"]) != 1
                or any(s["attribution"].get(k) for k in ("unverified_model", "assist_error"))):
            continue
        a, b = m["fields"]
        c, d = m["steps"]
        quality = (Decimal(a) / b + Decimal(c) / d) / 2
        eligible.append((-quality, Decimal(s["mean_estimated_usd_per_costed_call"]),
                         s["endpoint_latency"]["p95_ms"], name))
    if not eligible:
        return {"status": "FAIL", "model": None, "reason": "neither candidate passed safety, identity and usage checks"}
    return {"status": "PASS", "model": min(eligible)[-1],
            "reason": "safe development result ranked by mean field/step accuracy, then cost and endpoint p95"}


def fraction(obj, key):
    value = obj.get(key)
    return f"{value[0]}/{value[1]}" if value and value[1] else "—"


def show_ms(value):
    return str(value) if value is not None else "—"


def markdown(phase, summary):
    lines = [f"# M3 {phase}", "",
             "| Mode | PASS / FAIL / NOT_RUN cases | Correct fields | Correct steps | Invented params | Risky without approval | Assist p50 / p95 ms | API calls | Mean USD / costed call |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for mode, s in summary["modes"].items():
        m, l = s["product"], s["endpoint_latency"]
        counts = " / ".join(str(s["case_status"].get(v, 0)) for v in ("PASS", "FAIL", "NOT_RUN"))
        vals = [mode, counts, fraction(m, "fields"), fraction(m, "steps"), m.get("invented_parameters", "—"),
                m.get("risky_without_approval", "—"), f"{show_ms(l['p50_ms'])} / {show_ms(l['p95_ms'])}",
                s["calls"], s["mean_estimated_usd_per_costed_call"] or "—"]
        lines.append("| " + " | ".join(map(str, vals)) + " |")
    lines += ["", "| Mode | Provider-only gap mappings | Provider-only step classifications | Model contributed | Valid response, no contribution | Failed attempt, local fallback | Local only |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for mode, s in summary["modes"].items():
        m, a = s["provider_only"], s["attribution"]
        vals = [mode, fraction(m, "fields"), fraction(m, "candidate_steps"),
                *[a.get(k, 0) for k in ("model_contributed", "validated_no_contribution",
                                         "provider_failure_fallback", "local_only")]]
        lines.append("| " + " | ".join(map(str, vals)) + " |")
    sel = summary["selection"]
    lines += ["", f"Development selection **{sel['status']}**. {sel['model'] or 'No model selected'}. {sel['reason']}.",
              "", f"Cumulative known or conservatively reserved charges across phases: ${summary['campaign_exposure_usd']} of $5.",
              "", "Per-case outputs, expected labels, provider receipts and timing are in `cases.jsonl`. "
              "Counts, projection metrics and pricing coverage are in `summary.json`.", ""]
    return "\n".join(lines)


def write_report(directory: Path, phase, manifest, rows, summary):
    directory.mkdir(parents=True, exist_ok=True)
    files = {"manifest.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
             "cases.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
             "summary.json": json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
             "summary.md": markdown(phase, summary)}
    for name, content in files.items():
        path = directory / name
        if path.exists() and path.read_text() != content:
            raise ValueError(f"evidence already exists and differs: {path}")
    for name, content in files.items():
        path = directory / name
        if not path.exists():
            # Readers see a complete file if the process is interrupted during export.
            tmp = directory / (name + ".tmp")
            tmp.write_text(content)
            tmp.replace(path)
