"""Check declared delivery requirements with finite, reproducible scenarios."""

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from itertools import product
from zoneinfo import ZoneInfo

from alerting.clock import FixedClock
from alerting.contracts import CanonicalEvent, Severity
from alerting.routing import plan
from alerting.routing.directory import by_id, team_for
from alerting.rules.dsl import compile, number
from alerting.rules.engine import Ruleset, evaluate


def _leaves(node, positive=True):
    for key in ("all", "any"):
        if key in node:
            for child in node[key]:
                yield from _leaves(child, positive)
            return
    if "not" in node:
        yield from _leaves(node["not"], not positive)
    elif "field" in node:
        yield node, positive
    else:
        for path, value in node.items():
            yield {"field": path, "op": "eq", "value": value}, positive


def _boundaries(leaf):
    op, value = leaf["op"], leaf.get("value")
    if op in {"lt", "gt", "lte", "gte"}:
        if leaf["field"] == "$severity":
            return [severity.value for severity in Severity]
        n = number(value)
        if n is not None:
            return [int(v) if v == v.to_integral_value() else str(v) for v in (n, n + 1, n - 1)]
    if op in {"in", "not_in"}:
        outside = "other"
        while outside in value:
            outside += "_"
        return [*value, outside, None]
    if op == "glob":
        witness = re.sub(r"\[([^\]]+)\]", lambda m: m[1][0], value)
        return [witness.replace("*", "sample").replace("?", "x"), "", None]
    if op == "contains":
        return [[value], [], None]
    return [value, "other", None]


def _candidates(conditions):
    # Start with a common witness, then vary each field around its declared boundaries.
    probes, checks = defaultdict(list), defaultdict(list)
    for leaf, positive in conditions:
        path = leaf["field"]
        probes[path].extend(_boundaries(leaf))
        checks[path].append((compile(leaf), positive))
    prototype = {}
    for path, values in probes.items():

        def score(value):
            return sum(predicate({path: value})[0] == wanted for predicate, wanted in checks[path])

        prototype[path] = max(values, key=score)
    yield prototype
    for path, values in probes.items():
        for value in values:
            yield {**prototype, path: value}


def _targets(to, users, teams):
    kind, _, name = to.partition(":")
    team = teams.get(name, {})
    ids = {
        "team": team.get("members", []),
        "user": [name],
        "oncall": [team.get("oncall", {}).get("primary")],
    }.get(kind, [])
    return {user_id for user_id in ids if user_id in users}


def _snapshot(op, severity):
    return {
        "id": 1,
        "rule_id": op.rule["id"],
        "rule_version": op.rule.get("version", 1),
        "alert_key": op.key,
        "status": "open",
        "severity": severity,
        "title": op.title,
        "body": op.body,
        "fields": op.fields,
    }


def _requirements(routes, people, team_id, policy, severity, intents):
    channels = {intent.channel.value for intent in intents}
    for route in routes:
        to = route["to"]
        if not people[to]:
            yield "route target", f"{to} reaches nobody"
        declared = route.get("channels", "preferences")
        if declared == "preferences":
            if severity == "critical" and not channels:
                yield "critical preferences", f"{to} has no reachable channel"
        else:
            actual = {
                i.channel.value for i in intents if i.recipient in people[to] or i.recipient == to
            }
            for channel in sorted(set(declared) - actual):
                yield "explicit channels", f"{to} has no usable {channel} destination"
    for channel in sorted(set(policy.get(severity, [])) - channels):
        yield "team required_channels", f"{team_id} requires {channel}"


def check(rule_draft, ruleset, schemas, directory):
    """Check the draft in isolation. Schema `samples` hold canonical field maps or {fields: ...}."""
    rule = {"id": "draft", **rule_draft, "enabled": True}
    checked, violations, warnings = [], [], []
    report = {"scenarios_checked": checked, "violations": violations, "warnings": warnings}

    routes = rule.get("route", [])
    users, teams = by_id(directory["users"]), by_id(directory["teams"])
    people = {route["to"]: _targets(route["to"], users, teams) for route in routes}
    team_id = team_for(rule.get("owner", ""), directory)
    policy = teams.get(team_id, {}).get("required_channels", {})
    missing_targets = [to for to in people if not people[to]]

    def record(scenario, requirement, problem, trace):
        violations.append(
            dict(scenario=scenario, requirement=requirement, problem=problem, trace=trace)
        )

    match = rule.get("match", {})
    try:
        predicate = compile(match)
    except (ValueError, TypeError) as exc:
        if str(exc) != "regex exceeds 200 characters":
            record({}, "valid rule", str(exc), [])
            return report
        predicate = None
    conditions = list(_leaves(match))
    for leaf, _ in conditions:
        pattern = leaf.get("value")
        if leaf["op"] == "regex" and (
            len(pattern) > 200 or re.search(r"\([^)]*[*+{|][^)]*\)[*+{]", pattern)
        ):
            warnings.append({"problem": "regex too long or complex", "field": leaf["field"]})
    if warnings or predicate is None:
        for to in missing_targets:
            record({}, "route target", f"{to} reaches nobody", [])
        return report

    entries = list(schemas.values()) if isinstance(schemas, dict) else list(schemas)
    registry = dict(ruleset.schemas)
    samples = []
    environments = set()
    for schema in entries:
        schema_id = schema["id"]
        registry[schema_id] = {**registry.get(schema_id, {}), **schema}
        environments.update(schema.get("environments", []))
        samples.extend((schema_id, s.get("fields", s)) for s in schema.get("samples", []))
    if not any(predicate(fields)[0] for _, fields in samples):
        warnings.append({"problem": "never fires", "samples_checked": len(samples)})
    trial = Ruleset([rule], registry.values())
    candidates = [(None, fields) for fields in _candidates(conditions)] + samples
    environments.update(
        fields["$environment"]
        for _, fields in candidates
        if isinstance(fields.get("$environment"), str)
    )
    severities = {"critical", "error", "warning"} | {
        fields["$severity"]
        for _, fields in candidates
        if isinstance(fields.get("$severity"), str) and fields["$severity"] in Severity
    }
    recipients = sorted({user_id for group in people.values() for user_id in group})
    zones = sorted({users[uid].get("timezone", "UTC") for uid in recipients}) or ["UTC"]
    directories = {None: directory}
    for absent in recipients:
        directories[absent] = {
            **directory,
            "users": [
                {**user, "contacts": {}} if user["id"] == absent else user
                for user in directory["users"]
            ],
        }
    reference = FixedClock(datetime(2026, 1, 15, tzinfo=UTC))
    seen = set()
    for (schema_id, candidate), severity, environment in product(
        candidates, sorted(severities), sorted(environments) or [None]
    ):
        fields = {**candidate, "$severity": severity}
        if environment is not None:
            fields["$environment"] = environment
        signature = (schema_id, json.dumps(fields, sort_keys=True))
        if signature in seen:
            continue
        seen.add(signature)
        event = CanonicalEvent(
            source_id="preview",
            transport="http",
            payload_hash="",
            schema_id=schema_id,
            raw={},
            fields=fields,
            severity=severity,
            received_at=reference.now(),
        )
        result = evaluate(event, trial, {}, reference)
        if not result.alert_ops:
            checked.append({"fields": fields, "matched": False})
            continue
        op = result.alert_ops[0]
        trace = result.decisions[0]["reason"]["trace"]
        alert = _snapshot(op, severity)
        transition = "digest_due" if rule.get("group", {}).get("mode") == "digest" else op.transition
        for zone, minute, absent in product(zones, (479, 480, 1199, 1200, 1390), directories):
            clock = FixedClock(
                datetime(2026, 1, 15, minute // 60, minute % 60, tzinfo=ZoneInfo(zone))
            )
            scenario = {
                "fields": fields,
                "matched": True,
                "timezone": zone,
                "local_time": clock.now().isoformat(),
                "missing_contact": absent,
            }
            checked.append(scenario)
            intents = plan(alert, transition, rule, fields, directories[absent], [], {}, clock).intents
            for requirement, problem in _requirements(
                routes, people, team_id, policy, severity, intents
            ):
                record(scenario, requirement, problem, trace)
    if not any(scenario["matched"] for scenario in checked):
        for to in missing_targets:
            record({}, "route target", f"{to} reaches nobody", [])
    return report
