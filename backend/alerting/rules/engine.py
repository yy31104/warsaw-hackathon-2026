"""Evaluate all rules against a caller-owned view of open alerts. No side effects."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite

from alerting.contracts import SEVERITY_ORDER, CanonicalEvent, Severity, Transition
from alerting.ingest.normalize import pre_transform_fields
from alerting.rules.dsl import compile
from alerting.rules.failures import evaluating
from alerting.rules.silences import active_silence
from alerting.templates import lookup, render, variables


def duration(value):
    if isinstance(value, timedelta):
        return value
    match = re.fullmatch(r"(\d+)([smhd])", str(value))
    if not match:
        raise ValueError(f"invalid duration: {value}")
    return timedelta(seconds=int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]])


class Ruleset:
    def __init__(self, rules, schemas=()):
        self.rules = []
        self.shadow_rules = []
        for rule in rules:
            if rule.get("enabled", True):
                try:
                    predicate = compile(rule.get("match", {}))
                except (TypeError, ValueError) as exc:

                    def predicate(fields, reason=str(exc)):
                        raise ValueError(reason)

                destination = self.shadow_rules if rule.get("shadow") is True else self.rules
                destination.append((rule, predicate))
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
    effective_severity: Severity | None = None
    spike_state: dict | None = None
    routing_transition: Transition | None = None


@dataclass
class Evaluation:
    decisions: list = field(default_factory=list)
    alert_ops: list = field(default_factory=list)


def grouping(rule, schema, fields):
    transforms = schema.get("transforms")
    key_fields = pre_transform_fields(fields, transforms) if transforms else fields
    paths = rule.get("group", {}).get("by", [])
    if paths:
        values = {p: lookup(fields, p) for p in paths}
        return "|".join(
            render("{{" + p + "}}", key_fields).replace("%", "%25").replace("|", "%7C") for p in paths
        ), values
    return render(schema.get("alert_key") or "{{$schema}}|{{$service}}|{{$severity}}", key_fields), {}


def episode_for(schema, fields):
    if fields.get("$episode") is not None:
        return str(fields["$episode"])
    template = schema.get("episode_key")
    if template and all(lookup(fields, path) is not None for path in variables(template)):
        return render(template, fields)
    return None


def resolved_episode_id(state, snapshot, rule_id, key, episode):
    if snapshot and snapshot.get("status") == "resolved" and snapshot.get("latest_episode") == episode:
        return snapshot.get("id")
    if hasattr(state, "get_resolved_episode"):
        return state.get_resolved_episode(rule_id, key, episode)
    return None


def observe_spike(config, prior, at, severity):
    """Project a count into its clock window. The baseline uses only completed windows."""
    if not isinstance(config, dict):
        raise ValueError("escalate_on_spike requires min_count, factor and window")
    minimum, factor = config["min_count"], config["factor"]
    window = duration(config["window"])
    if type(minimum) is not int or minimum < 1:
        raise ValueError("escalate_on_spike.min_count must be a positive integer")
    if type(factor) not in (int, float) or not isfinite(factor) or factor <= 1:
        raise ValueError("escalate_on_spike.factor must be finite and greater than one")
    if window <= timedelta():
        raise ValueError("escalate_on_spike.window must be a positive duration")

    seconds = window.total_seconds()
    state = dict(prior or {})
    if state.get("window_seconds") != seconds:
        state = dict(
            window_seconds=seconds,
            started_at=at.isoformat(),
            bucket=0,
            count=0,
            baseline=None,
            last_spike_bucket=None,
        )
    bucket = (at - datetime.fromisoformat(state["started_at"])) // window
    elapsed = bucket - state["bucket"]
    if elapsed < 0:
        return state, severity, None
    if elapsed:
        # Weight the newest completed count by 1/4, then apply any empty windows at once.
        baseline, count = state["baseline"], state["count"]
        baseline = count if baseline is None else 0.75 * baseline + 0.25 * count
        state.update(bucket=bucket, count=0, baseline=baseline * 0.75 ** (elapsed - 1))
    state["count"] += 1
    count, baseline = state["count"], state["baseline"]
    if not baseline or count < minimum or count < factor * baseline:
        return state, severity, None

    if severity not in SEVERITY_ORDER or severity == Severity.critical:
        return state, severity, None
    last_spike = state["last_spike_bucket"]
    state["last_spike_bucket"] = bucket
    if last_spike is not None and bucket - last_spike <= 1:
        return state, severity, None
    next_rank = SEVERITY_ORDER[severity] + 1
    higher = next(level for level, rank in SEVERITY_ORDER.items() if rank == next_rank)
    evidence = dict(
        count=count, baseline=baseline, min_count=minimum, factor=factor, window_seconds=seconds
    )
    return state, higher, evidence


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
        with evaluating(rule):
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
            episode = episode_for(schema, fields)
            if not resolving and episode is not None:
                decision["reason"]["episode"] = episode
                resolved_id = resolved_episode_id(state, old, rule["id"], key, episode)
                if resolved_id is not None:
                    decision.update(outcome="LATE_OCCURRENCE", alert_id=resolved_id)
                    decision["reason"].update(transition=Transition.late_occurrence.value, alert_key=key)
                    continue
            if old and old.get("status") == "resolved":
                old = None
            severity = Severity(
                max(
                    event.severity,
                    (old or {}).get("severity", event.severity),
                    key=lambda level: SEVERITY_ORDER.get(level, -1),
                )
            )
            spike_state = None
            spike = rule.get("escalate_on_spike")
            if not resolving and spike is not None and spike is not False:
                spike_state, severity, evidence = observe_spike(
                    spike, (old or {}).get("spike_state"), clock.now(), severity
                )
                if evidence:
                    decision["reason"]["spike"] = evidence
            group = rule.get("group", {})
            digest = group.get("mode") == "digest"
            if resolving:
                if not old:
                    continue
                if schema.get("episode_key") and (
                    episode is None or episode != old.get("latest_episode")
                ):
                    decision.update(outcome="STALE_RESOLVE", alert_id=old.get("id"))
                    decision["reason"].update(
                        episode=episode, latest_episode=old.get("latest_episode")
                    )
                    continue
                transition = Transition.resolved
                outcome = "RESOLVED"
            elif not old:
                transition, outcome = Transition.created, "NEW_ALERT"
            elif group.get("renotify_on_severity_increase", True) and SEVERITY_ORDER.get(
                severity, -1
            ) > SEVERITY_ORDER.get(old["severity"], -1):
                transition, outcome = Transition.severity_increased, "EXISTING_ALERT"
            elif old.get("last_notified_at") and clock.now() - old["last_notified_at"] >= duration(
                group.get("cooldown", "10m")
            ):
                transition, outcome = Transition.cooldown_elapsed, "EXISTING_ALERT"
            else:
                transition, outcome = Transition.occurrence, "SUPPRESSED_COOLDOWN"
            notify = not digest and transition not in (Transition.occurrence, Transition.resolved)
            # Silenced matches count on the alert but never enter its pending delivery count.
            first_after_silence = (
                not digest and not resolving and old is not None and old.get("status") == "open"
                and old.get("last_notified_at") is None and old.get("count_since_notify") == 0
                and old.get("occurrence_count", 0) > 0
            )
            if first_after_silence:
                notify, outcome = True, "EXISTING_ALERT"
            if digest and not resolving:
                outcome = "DIGEST_QUEUED"
            if notify and not rule.get("route"):
                outcome = "UNROUTED"
            if not resolving and hasattr(state, "get_silences"):
                silence = active_silence(
                    state.get_silences(rule["id"]), fields, clock.now(), severity=severity
                )
                if silence:
                    notify, outcome = False, "SILENCED"
                    expiry = silence.expires_at.isoformat()
                    decision["reason"].update(
                        silence={"id": silence.id, "created_by": silence.created_by, "expires_at": expiry},
                        message=f"Silenced by {silence.created_by} until {expiry}",
                    )
            routing_transition = transition
            if first_after_silence and notify:
                routing_transition = Transition.created
                decision["reason"]["notification_cause"] = "first_eligible_after_silence"
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
                    effective_severity=severity,
                    spike_state=spike_state,
                    routing_transition=routing_transition,
                )
            )
    if not result.alert_ops and all(d["outcome"] == "NO_MATCH" for d in result.decisions):
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
