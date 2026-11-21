"""Build finite interpretation choices from a draft, submitted samples and one config snapshot."""

import re

from alerting.assist.schema_suggest import ranked_alias_paths
from alerting.contracts import SEVERITY_ORDER
from alerting.ingest.normalize import _severity, _time, normalize
from alerting.routing.directory import by_id, target_people, team_for
from alerting.routing.ladder import initial_routes
from alerting.rules.dsl import compile as compile_match, number
from alerting.templates import lookup, variables


def _axis(kind, path, values, *, label=None):
    axis = {"id": f"{kind}:{path}", "kind": kind, "path": path,
            "candidates": [{"value": value, "source": "heuristic"} for value in values]}
    if label is not None:
        axis.update(id=f"{axis['id']}:{label}", label=label)
    if kind == "scale":
        words = _field_words(path)
        minor_currency = bool(words & {"cent", "cents"}) or (
            "minor" in words and bool(words & {"amount", "price", "fee", "cost", "notional", "currency", "balance"})
        )
        for candidate in axis["candidates"]:
            factor = candidate["value"]
            suffix = " (e.g. cents to euros)" if factor == 0.01 and minor_currency else ""
            candidate["label"] = f"×{factor}{suffix}"
    elif kind == "severity":
        for candidate in axis["candidates"]:
            candidate["label"] = f"{label} → {candidate['value']}"
    return axis


def _field_words(path):
    leaf = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", path.split(".")[-1])
    return set(re.findall(r"[a-z]+", leaf.casefold()))


def _scale_values(path, value, time_path):
    numeric = number(value)
    if numeric is None:
        return []
    words = _field_words(path)
    time_like = path == time_path or bool(words & {"ms", "millis", "milliseconds", "latency", "duration", "timestamp"})
    unit_hint = bool(words & {"minor", "cents", "cent", "ms", "millis", "milliseconds"})
    # Unlabelled seven-digit amounts warrant checking their unit; numeric identifiers do not.
    large_measure = numeric.copy_abs() >= 1_000_000 and not words & {"id", "key", "uuid"}
    if not (unit_hint or large_measure):
        return []
    return [1, 0.01, 0.001] if time_like else [1, 0.01]


def _rule_scope(node, fields, unknown, time_paths):
    """Find time predicates in branches that can affect this sample's rule outcome."""
    if "not" in node:
        can_pass, can_fail, timed = _rule_scope(node["not"], fields, unknown, time_paths)
        return can_fail, can_pass, timed
    if "field" in node:
        path = node["field"]
        if path in time_paths or path in unknown or node["op"] == "regex":
            # Scope discovery never executes a configured regex against producer values.
            return True, True, path in time_paths
        matched, _ = compile_match(node)(fields)
        return matched, not matched, False
    if "any" in node:
        children = node["any"]
    elif "all" in node:
        children = node["all"]
    else:
        children = [{"field": path, "op": "eq", "value": value} for path, value in node.items()]
    parts = [_rule_scope(child, fields, unknown, time_paths) for child in children]
    if "any" in node:
        passes, fails = any(p[0] for p in parts), all(p[1] for p in parts)
        return passes, fails, fails and any(p[2] for p in parts)
    passes, fails = all(p[0] for p in parts), any(p[1] for p in parts)
    return passes, fails, passes and any(p[2] for p in parts)


def _timed_preferences(rule, directory, severities):
    users, teams = by_id(directory["users"]), by_id(directory["teams"])
    for route, _ in initial_routes(rule):
        if route.get("channels", "preferences") != "preferences":
            continue
        target = route["to"]
        team = teams.get(team_for(target, directory), {})
        for person in target_people(target, teams, users):
            if person not in users:
                continue
            rows = [*users[person].get("preferences", []), *team.get("preferences", []),
                    *directory.get("default_preferences", [])]
            for severity in severities:
                for row in rows:
                    if SEVERITY_ORDER.get(severity, -1) >= SEVERITY_ORDER.get(row.get("min_severity", "debug"), 0):
                        if row.get("when"):
                            return True
                        break  # This unconditional row wins before any later schedule.
    return False


def _time_matters(event, path, axes, snapshot, draft):
    unknown = set()
    severities = {event.severity.value}
    for axis in axes:
        field, kind = axis["path"], axis["kind"]
        if kind == "scale":
            unknown.add(field)
        elif kind == "alias":
            unknown.add("$" + field)
            if field == "severity":
                severities.update(SEVERITY_ORDER)
        elif kind == "severity" and (not axis.get("label") or str(lookup(event.fields, field)) == axis["label"]):
            unknown.add("$severity")
            severities.update(candidate["value"] for candidate in axis["candidates"])
    rules = snapshot["rules"] if isinstance(snapshot, dict) else snapshot.rules
    directory = snapshot["directory"] if isinstance(snapshot, dict) else snapshot.directory
    for rule in rules:
        if not rule.get("enabled", True) or rule.get("shadow"):
            continue
        can_match, _, timed = _rule_scope(rule.get("match", {}), event.fields, unknown, {"$time", path})
        grouping = rule.get("group", {}).get("by") or variables(draft.get("alert_key"))
        if timed or (can_match and ("$time" in grouping or _timed_preferences(rule, directory, severities))):
            return True
    return False


def _merge_declared(automatic, declared, rankings):
    axes = {(axis["kind"], axis["path"], axis.get("label") if axis["kind"] == "severity" else None): axis
            for axis in automatic}
    for item in declared:
        axis = dict(item)
        kind, path, label = axis["kind"], axis["path"], axis.get("label")
        key = kind, path, label if kind == "severity" else None
        if kind == "severity" and label is None:
            axes = {old: existing for old, existing in axes.items() if old[:2] != key[:2]}
        axis.setdefault("id", ":".join(str(part) for part in key if part is not None))
        options = axis["candidates"]
        if kind == "alias" and len(options) == 1 and options[0]["source"] != "declared":
            # A suggestion prefilled by onboarding is still an open question.
            alternate = next((p for p in rankings.get(path, []) if p != options[0]["value"]), None)
            if alternate:
                axis["candidates"] = [*options, {"value": alternate, "source": "heuristic"}]
        axes[key] = axis
    return list(axes.values())


def generate_hypotheses(draft, samples, snapshot, declared=(), *, clock):
    """Preserve reviewer and upstream suggestion provenance; never call a model."""
    aliases = draft.get("aliases", {})
    time_path = aliases.get("time")
    transforms = draft.get("transforms", {})
    events, scales = [], {}
    for sample in samples:
        source = {"id": sample.get("source", "preview"), "transport": sample.get("transport", "http")}
        event = normalize(sample["event"], source, [draft], received_at=clock.now())
        events.append(event)
        for path, value in event.fields.items():
            if path.startswith("$") or path in transforms or (path.endswith(".raw") and path[:-4] in transforms):
                continue
            factors = _scale_values(path, value, time_path)
            if factors:
                scales[path] = factors
    axes = [_axis("scale", path, values) for path, values in sorted(scales.items())]
    rankings = ranked_alias_paths([event.fields for event in events])
    for alias, paths in sorted(rankings.items()):
        selected = aliases.get(alias)
        options = list(dict.fromkeys(([selected] if selected else []) + paths))[:2]
        if len(options) == 2:
            axis = _axis("alias", alias, options)
            if selected:
                axis["candidates"][0]["source"] = "declared"
            axes.append(axis)
    # Check diagnostics for each plausible alias, including alternatives to the draft mapping.
    chosen = {alias: [path] for alias, path in aliases.items()}
    for axis in _merge_declared(axes, declared, rankings):
        if axis["kind"] == "alias":
            chosen[axis["path"]] = [candidate["value"] for candidate in axis["candidates"]]
    ranks, naive = set(), []
    for event in events:
        if event.schema_id is None:
            continue
        for path in chosen.get("severity", []):
            value = lookup(event.fields, path)
            if (isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "<redacted>"
                    and _severity(value, draft)[1] == "unmapped_severity"):
                ranks.add((path, str(value)))
        for path in chosen.get("time", []):
            if _time(lookup(event.fields, path), draft.get("time_format"))[1]:
                naive.append((event, path))
    axes.extend(_axis("severity", path, ["critical", "error", "warning", "info"], label=label)
                for path, label in sorted(ranks))
    axes = _merge_declared(axes, declared, rankings)
    explicit_zones = {axis["path"] for axis in declared if axis["kind"] == "timezone"}
    for event, path in naive:
        if path not in explicit_zones and _time_matters(event, path, axes, snapshot, draft):
            axes.append(_axis("timezone", path, ["UTC", "Europe/Warsaw"]))
            explicit_zones.add(path)
    return axes
