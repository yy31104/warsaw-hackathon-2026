"""Bounded, in-memory counterfactuals for one alerting rule."""

from collections import Counter
from copy import deepcopy
from decimal import Decimal
from difflib import unified_diff
from math import ceil, inf
from zoneinfo import ZoneInfo

import yaml

from alerting.rules.dsl import number
from alerting.rules.safety import _leaves
from alerting.simulate.history import replay_history
from alerting.simulate.protected import ProtectedScenarios


WARSAW = ZoneInfo("Europe/Warsaw")
OBJECTIVES = (
    "planned_interruptions", "night_interruptions",
    "added_delay_p95_s", "added_delay_max_s", "diff_size",
)
LIMITS = [
    "Finite history. Replay starts from empty state and uses the selected rule and current configuration.",
    "Planned, not delivered. Replay covers event-driven plans, digests and missed heartbeat deadlines in the sample.",
    "Live acknowledgement, scheduler reminders, escalation and manual actions are outside the replay.",
    "Delay metrics compare the first planned times for pairs present in both replays.",
    "Threshold scaling requires a single numeric comparison in the rule or reviewed proposal.",
]


def _threshold(rule):
    """Only alter a single, unambiguous numeric comparison named by the draft."""
    thresholds = [
        leaf for leaf, _ in _leaves(rule["match"])
        if leaf["op"] in {"gt", "gte", "lt", "lte"} and number(leaf.get("value")) is not None
    ]
    return thresholds[0] if len(thresholds) == 1 else None


def candidates(current, base):
    group = base.get("group") or {}
    cooldown = group.get("cooldown", "10m")
    mode = group.get("mode", "immediate")
    threshold = _threshold(base)
    multipliers = (Decimal("1"), Decimal("1.25"), Decimal("1.5")) if threshold else (Decimal("1"),)
    yielded = set()
    for candidate_cooldown in dict.fromkeys((cooldown, "5m", "10m", "30m")):
        for candidate_mode in ("immediate", "digest"):
            for multiplier in multipliers:
                draft = deepcopy(base)
                settings = deepcopy(group)
                if candidate_cooldown != cooldown:
                    settings["cooldown"] = candidate_cooldown
                if candidate_mode != mode:
                    settings["mode"] = candidate_mode
                if candidate_mode == "digest" and "every" not in settings:
                    settings["every"] = "5m"
                if settings:
                    draft["group"] = settings
                if multiplier != 1:
                    leaf = _threshold(draft)
                    value = number(leaf["value"]) * multiplier
                    if value != number(leaf["value"]):
                        leaf["value"] = int(value) if isinstance(leaf["value"], int) and value == value.to_integral_value() else str(value.normalize())
                signature = yaml.safe_dump(draft, sort_keys=True)
                if draft != current and signature not in yielded:
                    yielded.add(signature)
                    yield draft


def _first_plans(replay):
    first = {}
    for event_id, plans in replay.planned_deliveries.items():
        for recipient, channel, at in plans:
            key = event_id, recipient, channel
            first[key] = min(first.get(key, at), at)
    return first


def _pair_diff(before, after):
    counts = Counter()
    changes, delays = [], []
    for event_id, recipient, channel in sorted(before.keys() | after.keys()):
        key = event_id, recipient, channel
        old, new = before.get(key), after.get(key)
        if old is not None and new is not None:
            delay_s = (new - old).total_seconds()
            delays.append(max(0, delay_s))
            if delay_s == 0:
                continue
            kind = "delayed" if delay_s > 0 else "earlier"
        else:
            delay_s = None
            kind = "added" if old is None else "missing"
        counts[kind] += 1
        if len(changes) < 20:
            changes.append({
                "event_id": event_id, "recipient": recipient, "channel": channel, "kind": kind,
                "before_at": old, "after_at": new, "delay_s": delay_s,
            })
    delays.sort()
    return {
        "counts": dict(counts), "examples": changes,
        "compared_pairs": len(delays),
    }, delays


def _interruptions(replay):
    total, night = Counter(), Counter()
    for recipient, channel, at in replay.planned_sends:
        if channel in {"sms", "phone"}:
            total[recipient] += 1
            hour = at.astimezone(WARSAW).hour
            if hour >= 22 or hour < 7:
                night[recipient] += 1
    return total, night


def _config_diff(current, draft):
    before = yaml.safe_dump(current, sort_keys=True).splitlines()
    after = yaml.safe_dump(draft, sort_keys=True).splitlines()
    return [line for line in list(unified_diff(before, after, n=0))[2:] if line[:1] in "+-"]


def _objective(option):
    return tuple(
        option["metrics"][name] if option["metrics"][name] is not None else inf
        for name in OBJECTIVES
    )


def _rank(option):
    return _objective(option), tuple(option["diff"]), yaml.safe_dump(option["draft"], sort_keys=True)


def pareto_options(options):
    """Sort deterministically and collapse equivalent metric vectors."""
    unique = {}
    for option in sorted(options, key=_rank):
        unique.setdefault(_objective(option), option)
    frontier = [
        option for score, option in unique.items()
        if not any(other != score and all(b <= a for a, b in zip(score, other, strict=True))
                   for other in unique)
    ]
    return frontier[:3]


def optimize_history(events, current, base, schemas, directory, runbooks, acted_on=(), *, edited=False):
    prior = replay_history(events, current, schemas, directory, runbooks)
    first_before = _first_plans(prior)
    before, before_night = _interruptions(prior)
    protected = ProtectedScenarios(current, schemas, directory, runbooks, acted_on=acted_on)

    def evaluate(draft):
        replay = replay_history(events, draft, schemas, directory, runbooks)
        after, after_night = _interruptions(replay)
        total, night = after.total(), after_night.total()
        pair_diff, delays = _pair_diff(first_before, _first_plans(replay))
        diff = _config_diff(current, draft)
        protections, counterexample = protected.check(draft, prior, replay)
        by_recipient = [
            {"recipient": recipient, "planned_interruptions": before[recipient] - after[recipient],
             "night_interruptions": before_night[recipient] - after_night[recipient]}
            for recipient in sorted(before.keys() | after.keys())
            if before[recipient] != after[recipient] or before_night[recipient] != after_night[recipient]
        ]
        return {
            "draft": draft, "diff": diff, "pair_changes": pair_diff,
            "metrics": {
                "planned_interruptions": total, "night_interruptions": night,
                "added_delay_p95_s": delays[ceil(0.95 * len(delays)) - 1] if delays else None,
                "added_delay_max_s": delays[-1] if delays else None, "diff_size": len(diff),
            },
            "savings": {
                "planned_interruptions": before.total() - total,
                "night_interruptions": before_night.total() - night,
                "by_recipient": by_recipient,
            },
            **protections, "protected_violations": int(counterexample is not None),
            "counterexample": counterexample,
        }

    results = [evaluate(draft) for draft in (iter((base,)) if edited else candidates(current, base))]
    rejected = [row for row in results if row["counterexample"]]
    best_rejected = min(rejected, key=_rank, default=None)
    options = [] if edited else pareto_options([
        row for row in results if not row["counterexample"] and (
            row["savings"]["planned_interruptions"] > 0 or (
                row["savings"]["planned_interruptions"] == 0 and row["savings"]["night_interruptions"] > 0
            )
        )
    ])
    response = {
        "evaluated_count": len(results), "events_replayed": len(events),
        "options": [{"id": label, **row} for label, row in zip("BCD", options)],
        "best_rejected": {"id": "A", **best_rejected} if best_rejected else None,
        "acted_on_incidents_reviewed": len({row["alert_id"] for row in acted_on}),
        "baseline": {"planned_interruptions": before.total(), "night_interruptions": before_night.total()},
        "limits": LIMITS,
    }
    if edited:
        response["draft_result"] = {"id": "Edited draft", **results[0]}
    return response
