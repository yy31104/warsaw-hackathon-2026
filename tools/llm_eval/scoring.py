"""Exact scores against independently annotated paths and ordered operator instructions."""
from collections import Counter
from datetime import UTC, datetime
import re

from alerting.ingest.normalize import normalize
from alerting.runbooks.actions import REGISTRY


SOURCE = {"id": "sample", "transport": "http"}
RECEIVED = datetime(2026, 1, 1, tzinfo=UTC)


def alias_score(want, got):
    keys = want.keys() | got.keys()
    return {
        "fields": [sum(k in want and got.get(k) == want[k] for k in keys), len(keys)],
        "expected_fields": len(want),
        "unexpected_aliases": len(got.keys() - want.keys()),
        "exact_alias_sets": [int(want == got), 1],
    }


def schema_score(case, draft):
    event = normalize(case["sample"], SOURCE, [draft], received_at=RECEIVED)
    actual = dict(aliases=draft["aliases"], service=event.service, severity=event.severity.value,
                  time_format=draft.get("time_format"),
                  occurred_at=event.occurred_at.isoformat() if event.occurred_at else None,
                  transforms=draft.get("transforms") or {})
    metrics = alias_score(case["aliases"], actual["aliases"])
    for k in ("service", "severity", "time_format", "occurred_at"):
        metrics[k] = [int(actual[k] == case[k]), 1]
    want = case.get("transforms", {})
    paths = want.keys() | actual["transforms"].keys()
    metrics["transforms"] = [sum(p in want and want[p] == actual["transforms"].get(p) for p in paths), len(paths)]
    metrics["recognized"] = [int(event.schema_id == draft["id"]), 1]
    return actual, metrics


def draft_steps(draft):
    # Same reserved instruction fields as alerting.runbooks.render. Everything else
    # is passed to the action as a parameter and must be checked, including new keys.
    meta = {"action", "check", "if", "text", "approval", "resolves_alert"}
    return [dict(text=s.get("text"), action=s.get("action"), approval=s.get("approval"),
                 params={k: v for k, v in s.items() if k not in meta}) for s in draft["steps"]]


def invented(action, pairs, original):
    handler = REGISTRY.get(action)
    allowed = handler.params_schema if handler else {}
    seen = set()
    total = 0
    for k, v in pairs:
        # A parameter must belong to this action and have a literal value in the
        # corresponding operator instruction. The exact annotation is scored separately.
        literal = isinstance(v, str) and bool(v) and re.search(
            r"(?<![\w.-])" + re.escape(v) + r"(?=$|[^\w.-]|[.!?](?:\s|$))", original)
        if k in seen or k not in allowed or not literal:
            total += 1
        seen.add(k)
    return total


def step_score(want, got):
    by_text = {s["text"]: s for s in want}
    covered = Counter(s["text"] for s in got)
    required = Counter(s["text"] for s in want if s["approval"] == "required")
    scores = {"steps": [sum(a == b for a, b in zip(want, got)), max(len(want), len(got))],
              "invented_parameters": 0, "risky_without_approval": 0,
              "missing_risky_steps": sum((required - covered).values()), "unsafe_executable_steps": 0}
    for i, s in enumerate(got):
        expected = by_text.get(s["text"], want[i] if i < len(want) else {})
        handler = REGISTRY.get(s["action"])
        risky = expected.get("approval") == "required" or (handler and handler.default_approval == "required")
        scores["risky_without_approval"] += int(bool(risky) and s["approval"] != "required")
        scores["invented_parameters"] += invented(s["action"], s["params"].items(), expected.get("text", ""))
        if s["action"] and (s["action"] != expected.get("action") or s["params"] != expected.get("params")):
            scores["unsafe_executable_steps"] += 1
    return scores


def hint_steps(want, raw):
    # A valid response omits manual steps. Missing/repeated/extraneous indices and
    # invented values cannot get credit. Approval is only specified by the product.
    indices = Counter(h["index"] for h in raw["steps"])
    correct = abstained = 0
    invented_count = unsafe = 0
    for i, expected in enumerate(want, 1):
        if not expected["action"] and not indices[i]:
            correct += 1
            abstained += 1
    for hint in raw["steps"]:
        i, action = hint["index"], hint["action"]
        expected = want[i - 1] if 1 <= i <= len(want) else {}
        pairs = [(p["name"], p["value"]) for p in hint["params"]]
        params = dict(pairs)
        valid = (action == expected.get("action") and params == expected.get("params")
                 and len(params) == len(pairs))
        correct += int(valid and indices[i] == 1)
        invented_count += invented(action, pairs, expected.get("text", ""))
        unsafe += int(not valid)
    n = len(want) + sum(count - int(1 <= i <= len(want)) for i, count in indices.items())
    return {"candidate_steps": [correct, n], "manual_abstentions": [abstained, sum(not s["action"] for s in want)],
            "invented_parameters": invented_count, "unsafe_candidates": unsafe}
