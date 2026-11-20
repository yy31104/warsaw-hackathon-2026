"""Evaluate all rules against a caller-owned view of open alerts. No side effects."""

import re
from dataclasses import dataclass, field
from datetime import timedelta

from alerting.contracts import SEVERITY_ORDER, CanonicalEvent, Transition
from alerting.rules.dsl import compile
from alerting.templates import lookup, render


def duration(value):
    if isinstance(value, timedelta):
        return value
    match = re.fullmatch(r"(\d+)([smhd])", str(value))
    if not match:
        raise ValueError(f"invalid duration: {value}")
    return timedelta(seconds=int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]])


class Ruleset:
    def __init__(self, rules, schemas=()):
        self.rules = [(r, compile(r.get("match", {}))) for r in rules if r.get("enabled", True)]
        self.schemas = {s["id"]: s for s in schemas}
        self.resolvers = {
            s["id"]: compile(s["resolve_when"]) for s in schemas if s.get("resolve_when")
        }


@dataclass
class AlertOp:
    rule: dict
    key: str
    transition: Transition
    group_values: dict
    title: str
    body: str
    fields: dict
    notify: bool


@dataclass
class Evaluation:
    decisions: list = field(default_factory=list)
    alert_ops: list = field(default_factory=list)


def grouping(rule, schema, fields):
    paths = rule.get("group", {}).get("by", [])
    if paths:
        values = {p: lookup(fields, p) for p in paths}
        return "|".join(
            render("{{" + p + "}}", fields).replace("%", "%25").replace("|", "%7C") for p in paths
        ), values
    return render(schema.get("alert_key") or "{{$schema}}|{{$service}}|{{$severity}}", fields), {}


def evaluate(event, ruleset, state, clock):
    if not isinstance(event, CanonicalEvent):
        event = CanonicalEvent.model_validate(event)
    if not isinstance(ruleset, Ruleset):
        ruleset = Ruleset(**ruleset) if isinstance(ruleset, dict) else Ruleset(ruleset)
    schema = ruleset.schemas.get(event.schema_id, {})
    fields = event.fields
    resolver = ruleset.resolvers.get(event.schema_id)
    resolving = resolver and resolver(fields)[0]
    result = Evaluation()
    for rule, predicate in ruleset.rules:
        matched, trace = predicate(fields)
        decision = dict(
            rule_id=rule["id"],
            rule_version=rule.get("version", 1),
            outcome="NO_MATCH",
            reason={"trace": trace, "matched": matched},
        )
        result.decisions.append(decision)
        if not matched and not resolving:
            continue
        key, values = grouping(rule, schema, fields)
        old = (
            state.get_open(rule["id"], key)
            if hasattr(state, "get_open")
            else state.get((rule["id"], key))
        )
        if old is not None and hasattr(old, "model_dump"):
            old = old.model_dump()
        if old and old.get("status") == "resolved":
            old = None
        group = rule.get("group", {})
        digest = group.get("mode") == "digest"
        if resolving:
            if not old:
                continue
            transition = Transition.resolved
            outcome = "RESOLVED"
        elif not old:
            transition, outcome = Transition.created, "NEW_ALERT"
        elif group.get("renotify_on_severity_increase", True) and SEVERITY_ORDER.get(
            event.severity, -1
        ) > SEVERITY_ORDER.get(old["severity"], -1):
            transition, outcome = Transition.severity_increased, "EXISTING_ALERT"
        elif old.get("last_notified_at") and clock.now() - old["last_notified_at"] >= duration(
            group.get("cooldown", "10m")
        ):
            transition, outcome = Transition.cooldown_elapsed, "EXISTING_ALERT"
        else:
            transition, outcome = Transition.occurrence, "SUPPRESSED_COOLDOWN"
        notify = not digest and transition not in (Transition.occurrence, Transition.resolved)
        if digest and not resolving:
            outcome = "DIGEST_QUEUED"
        if notify and not rule.get("route"):
            outcome = "UNROUTED"
        decision.update(outcome=outcome)
        decision["reason"].update(transition=transition.value, alert_key=key)
        present = rule.get("present", {})
        result.alert_ops.append(
            AlertOp(
                rule,
                key,
                transition,
                values,
                render(present.get("title") or rule["id"], fields),
                render(present.get("body"), fields),
                {p: lookup(fields, p) for p in present.get("fields", [])},
                notify,
            )
        )
    if not result.alert_ops:
        if not result.decisions:
            result.decisions.append(
                dict(rule_id=None, rule_version=None, outcome="NO_MATCH", reason={})
            )
        if SEVERITY_ORDER.get(event.severity, -1) >= SEVERITY_ORDER["error"]:
            result.decisions.append(
                dict(
                    rule_id=None,
                    rule_version=None,
                    outcome="UNROUTED",
                    reason={"cause": "NO_MATCH"},
                )
            )
    return result
