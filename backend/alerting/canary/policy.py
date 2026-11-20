"""Source isolation predicate for the shared worker and rule evaluator (W0/W1)."""


def allows_rule(event, rule):
    reserved_source = event.source_id == "canary"
    reserved_rule = rule["id"] == "delivery-canary"
    synthetic = event.trace.get("synthetic") is True
    if reserved_source or reserved_rule:
        return reserved_source and reserved_rule and synthetic and rule.get("include_synthetic") is True
    return not synthetic or rule.get("include_synthetic") is True
