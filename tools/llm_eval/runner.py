"""Run the production assist functions on one fixed synthetic case at a time."""
import json
import os
from collections import OrderedDict
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from alerting import llm
from alerting.assist import runbook_structure, schema_suggest, text_input
from alerting.ingest.normalize import normalize

from .ledger import dated_snapshot
from .scoring import RECEIVED, SOURCE, alias_score, draft_steps, hint_steps, schema_score, step_score


HERE = Path(__file__).resolve().parent
RATE_CARD = json.loads((HERE / "rates.json").read_text())
OUTPUT_CAP = 1536
OFFLINE = "heuristic"


async def discover(ids):
    """Only the SDK model inventory is accessed here. Paid content always goes through M2."""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return {}, "OPENAI_API_KEY unavailable"
    if os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1":
        return {}, "rate card requires the global OpenAI endpoint"
    try:
        from openai import AsyncOpenAI
        async with AsyncOpenAI(max_retries=0, timeout=8.0) as client:
            listed = {m.id async for m in client.models.list()}
    except Exception as e:
        # A proxy or SDK exception can contain headers, request fragments, or a key.
        return {}, f"model inventory failed ({type(e).__name__})"
    candidates = {}
    for family in ids:
        snapshots = sorted(m for m in listed if dated_snapshot(family, m))
        candidates[family] = {"listed_family": family in listed, "listed_snapshots": snapshots,
                              "selected": snapshots[-1] if snapshots else family if family in listed else None}
    return candidates, None


def not_run(mode, task, case_id, reason):
    return dict(mode=mode, task=task, case_id=case_id, status="NOT_RUN", reason=reason, attribution="not_run",
                product_source=None, product=None, provider_only=None, endpoint_latency_ms=None, call=None)


async def evaluate(phase, mode, task, case, journal, rate=None, offline_aliases=None):
    case_id = case["id"]
    key = (phase, mode, case_id)
    base = not_run(mode, task, case_id, None)
    raw = blocked = None
    count_seconds = 0.0
    original = llm.structured

    async def tracked(output_model, **kwargs):
        nonlocal raw, blocked, count_seconds
        if mode == OFFLINE:
            raise RuntimeError("heuristic evaluation attempted provider access")
        # An exact requested model and the reviewed, non-retrying M2 adapter are
        # required. No ranking or transport retry is permitted inside a case.
        if llm.status() != {"enabled": True, "provider": "openai", "model": mode}:
            raise RuntimeError("M2 selection changed")
        count_started = perf_counter()
        count = await llm.count_openai_input(output_model, system=kwargs["system"], user=kwargs["user"])
        count_seconds += perf_counter() - count_started
        try:
            journal.reserve(*key, rate, OUTPUT_CAP, counted=count, count_ms=count_seconds * 1000)
        except ValueError as e:
            blocked = str(e)
            return None
        with llm.observe_calls(lambda obs: journal.settle(*key, rate, obs), max_output_tokens=OUTPUT_CAP):
            result = await original(output_model, **kwargs)
        raw = result.model_dump(mode="json", exclude_none=True) if result is not None else None
        return result

    config = {"ASSIST_ENABLED": "0" if mode == OFFLINE else "1", "ASSIST_PROVIDER": "openai", "ASSIST_MODEL": mode}
    with (patch.dict(os.environ, config), patch.object(llm, "structured", tracked),
          patch.object(schema_suggest, "_hints", OrderedDict()), patch.object(text_input, "_cache", OrderedDict())):
        started = perf_counter()
        try:
            if task == "schema":
                result = await schema_suggest.suggest(normalize(case["sample"], SOURCE, [], received_at=RECEIVED))
            else:
                result = await runbook_structure.structure(case["text"])
        except Exception as e:
            return {**base, "status": "FAIL", "reason": f"production assist raised {type(e).__name__}",
                    "attribution": "assist_error", "call": journal.receipt(*key),
                    "endpoint_latency_ms": (perf_counter() - started - count_seconds) * 1000}
        elapsed = (perf_counter() - started - count_seconds) * 1000

    call = journal.receipt(*key)
    if blocked:
        return {**base, "reason": blocked, "call": call}
    source = result["source"]
    if mode == OFFLINE:
        attribution = "heuristic"
    elif call is None:
        attribution = "local_only"
    elif not call["model_verified"] and call["outcome"] == "success":
        attribution = "unverified_model"
    elif raw is None or call["outcome"] != "success":
        attribution = "provider_failure_fallback"
    else:
        attribution = "model_contributed" if source == "llm" else "validated_no_contribution"

    provider_scores = None
    valid_response = call and call["outcome"] == "success" and call["model_verified"] and raw is not None
    if task == "schema":
        actual, metrics = schema_score(case, result["draft"])
        if valid_response and offline_aliases is not None:
            gaps = {a: p for a, p in case["aliases"].items() if a not in offline_aliases}
            provider_scores = alias_score(gaps, raw)
    else:
        actual = draft_steps(result["draft"])
        metrics = step_score(case["steps"], actual)
        if valid_response:
            provider_scores = hint_steps(case["steps"], raw)
    exact = all(v[0] == v[1] for v in metrics.values() if isinstance(v, list))
    unsafe = any(metrics.get(k, 0) for k in ("invented_parameters", "risky_without_approval",
                                              "missing_risky_steps", "unsafe_executable_steps"))
    attribution_ok = ((source == "llm") == (attribution == "model_contributed")
                      and attribution not in ("unverified_model", "provider_failure_fallback"))
    return {**base, "status": "PASS" if exact and not unsafe and attribution_ok else "FAIL", "reason": None,
            "attribution": attribution, "product_source": source, "product_model": result.get("model"),
            "actual": actual, "product": metrics, "provider_only": provider_scores,
            "provider_hints": raw if valid_response else None, "endpoint_latency_ms": elapsed, "call": call}
