"""Bounded counterfactual verification and an exact, adaptive minimax questionnaire."""

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from functools import cache
from itertools import product
from math import prod
from time import monotonic
from zoneinfo import ZoneInfo

from alerting.clock import FixedClock
from alerting.ingest.normalize import flatten
from alerting.rules.engine import Ruleset
from alerting.rules.safety import verify
from alerting.templates import lookup


MAX_COMBINATIONS = 64
TIME_LIMIT_SECONDS = 2
CRITICAL_WARNING = "may page critical: keep in review"
REFERENCE_TIME = datetime(2026, 1, 15, 12, tzinfo=UTC)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _interpretation(draft, hypotheses, choices, samples):
    schema = deepcopy(draft)
    aliases = schema.setdefault("aliases", {})
    selected = [(h, h["candidates"][i]["value"]) for h, i in zip(hypotheses, choices, strict=True)]
    for hypothesis, value in selected:
        if hypothesis["kind"] == "alias":
            aliases[hypothesis["path"].removeprefix("$")] = value
    timezones = {}
    for hypothesis, value in selected:
        kind, path = hypothesis["kind"], hypothesis["path"]
        if kind == "scale":
            schema.setdefault("transforms", {}).setdefault(path, {})["scale"] = value
        elif kind == "timezone":
            timezones[path] = ZoneInfo(value)
        elif kind == "severity" and aliases.setdefault("severity", path) == path:
            label = hypothesis.get("label")
            labels = {label} if label is not None else {
                str(raw) for sample in samples if (raw := lookup(flatten(sample["event"]), path)) is not None
            }
            schema.setdefault("severity_map", {}).update(dict.fromkeys(labels, value))
    return schema, timezones


def _questions(hypotheses, worlds, outcomes, check_budget):
    # Each state is a subset of at most 64 worlds, represented by a bit mask.
    masks = []
    for index, hypothesis in enumerate(hypotheses):
        by_option = [0] * len(hypothesis["candidates"])
        for i, values in enumerate(worlds):
            by_option[values[index]] |= 1 << i
        masks.append(by_option)
    group_masks = defaultdict(int)
    for index, outcome in enumerate(outcomes):
        group_masks[outcome] |= 1 << index
    axes = sorted((i for i, options in enumerate(masks) if len(options) > 1),
                  key=lambda i: hypotheses[i]["id"])

    @cache
    def solve(state):
        check_budget()
        for group, members in group_masks.items():
            if state & members == state:
                return 0, {"outcome_group": group}
        best_depth, best_tree = len(axes) + 1, None
        for i in axes:
            parts = [(j, state & mask) for j, mask in enumerate(masks[i]) if state & mask]
            if len(parts) < 2:
                continue
            depth, branches = 0, []
            for option, subset in parts:
                child_depth, child = solve(subset)
                depth = max(depth, 1 + child_depth)
                if depth >= best_depth:
                    break
                branches.append({"option_index": option, "next": child})
            else:
                best_depth, best_tree = depth, {"question_id": hypotheses[i]["id"], "branches": branches}
        return best_depth, best_tree

    depth, tree = solve((1 << len(worlds)) - 1)
    # The tree is adaptive. The list gives its questions in breadth-first order for clients.
    by_id = {h["id"]: h for h in hypotheses}
    ordered = {}
    pending = deque([tree])
    while pending:
        node = pending.popleft()
        if "question_id" in node:
            id = node["question_id"]
            ordered.setdefault(id, by_id[id])
            pending.extend(branch["next"] for branch in node["branches"])
    return depth, tree, list(ordered.values())


def stability(samples, draft, hypotheses, snapshot, *, clock=None, deadline=None):
    """Compare independent samples against one configuration; candidate values are counterfactual only."""
    local_deadline = monotonic() + TIME_LIMIT_SECONDS
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline

    def check_budget():
        if monotonic() >= deadline:
            raise TimeoutError

    snapshot = snapshot if isinstance(snapshot, Mapping) else vars(snapshot)
    pinned = deepcopy({key: snapshot[key] for key in ("rules", "schemas", "directory", "runbooks")})
    pinned["sources"] = deepcopy(snapshot.get("sources", []))
    revision = snapshot["revision"]
    config_id = hashlib.sha256(_canonical([revision, pinned]).encode()).hexdigest()
    clock = FixedClock(clock.now() if clock is not None else REFERENCE_TIME)
    hypotheses = list(hypotheses)
    ids = [h["id"] for h in hypotheses]
    if len(set(ids)) != len(ids):
        raise ValueError("hypothesis IDs must be unique")
    for h in hypotheses:
        if h["kind"] not in {"alias", "scale", "severity", "timezone"} or not h["candidates"]:
            raise ValueError("each hypothesis needs a supported kind and at least one candidate")
    if not samples:
        raise ValueError("at least one verification sample is required")
    domains = [range(len(h["candidates"])) for h in hypotheses]
    total = prod(map(len, domains))
    report = {
        "status": "INCOMPLETE", "complete": False, "decision_invariant": None, "semantics_confirmed": False,
        "evaluated_combinations": 0, "total_combinations": total, "relevant_unknowns": [], "ordered_questions": [],
        "question_tree": None, "worst_case_questions": None, "outcome_groups": [], "hypotheses": hypotheses,
        "critical_possible": None, "warnings": [], "snapshot": {"id": f"sha256:{config_id}", "revision": revision},
    }
    if total > MAX_COMBINATIONS:
        return {**report, "reason": "combination_limit"}
    worlds, outcomes, groups = [], [], {}
    try:
        check_budget()
        ruleset = Ruleset(pinned["rules"], pinned["schemas"])
        base = draft.get("base", draft.get("id"))
        if "base" in draft and base not in ruleset.schemas:
            raise ValueError(f"unknown base schema: {base}")
        effective = {k: v for k, v in {**ruleset.schemas.get(base, {}), **draft}.items() if v is not None}
        sources = {source["id"]: source["transport"] for source in pinned["sources"]}
        samples = [{**({"transport": sources[s["source"]]} if s.get("source") in sources else {}), **s}
                   for s in samples]
        for choices in product(*domains):
            check_budget()
            schema, timezones = _interpretation(effective, hypotheses, choices, samples)
            result = verify(schema, samples, ruleset, pinned["directory"], pinned["runbooks"], clock,
                            include_outcome=True, timezones=timezones, check_budget=check_budget)
            rows = result["samples"]
            if any(row["critical_page"] for row in rows):
                report["critical_possible"] = True
                if total > 1:
                    report["warnings"] = [CRITICAL_WARNING]
            signature = [{"sample_index": i, **row["operational_outcome"]} for i, row in enumerate(rows)]
            key = _canonical(signature)
            if key not in groups:
                groups[key] = {"id": f"g{len(groups)}", "outcome": signature, "hypotheses": []}
            group = groups[key]
            group["hypotheses"].append(dict(zip(ids, choices, strict=True)))
            worlds.append(choices)
            outcomes.append(group["id"])
            report["evaluated_combinations"] += 1
        depth, tree, ordered = _questions(hypotheses, worlds, outcomes, check_budget)
        check_budget()
    except TimeoutError:
        return {**report, "reason": "time_limit"}
    return {
        **report, "complete": True, "decision_invariant": len(groups) == 1,
        "status": "INVARIANT" if len(groups) == 1 else "VARIANT",
        "critical_possible": bool(report["critical_possible"]), "outcome_groups": list(groups.values()),
        "ordered_questions": ordered, "question_tree": tree, "worst_case_questions": depth,
        "relevant_unknowns": [h["id"] for h in ordered],
    }
