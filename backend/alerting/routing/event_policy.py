"""Limit matching, recovery, shadow projection and heartbeat recording to eligible rules."""

from copy import copy

try:
    from alerting.canary.policy import allows_rule as _allows_rule
except ModuleNotFoundError as exc:
    if exc.name not in {"alerting.canary", "alerting.canary.policy"}:
        raise
    _allows_rule = None


def eligible_ruleset(event, ruleset):
    """Return a view of this event's already compiled rules, leaving the shared snapshot intact."""
    native_shadow = hasattr(ruleset, "shadow_rules")
    reserved = event.source_id == "canary" or event.trace.get("synthetic") is True

    def eligible(rule):
        if not native_shadow and rule.get("shadow") is True:
            return False
        if _allows_rule is None:
            # The isolated delivery checkout has no watchdog policy or trusted destination.
            return not reserved and rule["id"] != "delivery-canary"
        return _allows_rule(event, rule)

    def select(pairs):
        if all(eligible(rule) for rule, _ in pairs):
            return pairs
        return [(rule, predicate) for rule, predicate in pairs if eligible(rule)]

    live = select(ruleset.rules)
    drafts = getattr(ruleset, "shadow_rules", ())
    shadow = select(drafts)
    if live is ruleset.rules and shadow is drafts:
        return ruleset
    view = copy(ruleset)
    view.rules = live
    if native_shadow:
        view.shadow_rules = shadow
    return view
