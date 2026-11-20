"""Resolve opt-in ladder stages at offsets measured from the start of the incident."""

from datetime import timedelta

from alerting.rules.engine import duration


CHANNELS = ("ui", "email", "sms", "phone")


def explicit_targets(rule):
    return {
        route["to"] for route in rule.get("route", ())
        if route.get("channels", "preferences") != "preferences"
    }


def steps(rule):
    entries = rule.get("ladder")
    if not isinstance(entries, list) or not entries:
        return ()
    routes = rule.get("route", ())
    if not isinstance(routes, (list, tuple)) or any(
        not isinstance(route, dict) or not isinstance(route.get("to"), str) for route in routes
    ):
        return ()
    explicit = explicit_targets(rule)
    if all(route["to"] in explicit for route in routes):
        return ()
    result = []
    for entry in entries:
        if not isinstance(entry, str):
            return ()
        channel, at, interval = entry.partition("@")
        if channel not in CHANNELS or bool(result) != bool(at):
            return ()
        try:
            offset = duration(interval) if at else timedelta()
        except (ValueError, OverflowError):
            return ()
        if result and (CHANNELS.index(channel) <= CHANNELS.index(result[-1][0]) or offset <= result[-1][1]):
            return ()
        result.append((channel, offset))
    return tuple(result)


def step_rule(rule, channel):
    """Only preference targets participate in subsequent stages."""
    explicit = explicit_targets(rule)
    routes = [
        {**route, "channels": [channel]} for route in rule.get("route", ())
        if route["to"] not in explicit
    ]
    return {**rule, "route": routes, "_ladder_explicit_targets": sorted(explicit)}


def initial_routes(rule):
    routes = rule.get("route", ())
    if not rule.get("ladder"):
        return [(route, False) for route in routes]
    explicit = explicit_targets(rule)
    stages = steps(rule)
    result = []
    for route in routes:
        if route.get("channels", "preferences") != "preferences":
            result.append((route, False))
        elif route["to"] not in explicit:
            result.append(({**route, "channels": [stages[0][0]]}, True) if stages else (route, False))
    return result
