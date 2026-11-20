from math import log10

from alerting.rules.dsl import number
from alerting.templates import lookup


def _error_budget_burn(slo, fields):
    """A 1x burn earns one point, capped at 25 like impact. Counts cover one window."""
    if not isinstance(slo, dict) or not fields:
        return None
    requests_path, errors_path = slo.get("requests_total"), slo.get("errors_total")
    if not all(isinstance(path, str) and path for path in (requests_path, errors_path)):
        return None
    requests = number(lookup(fields, requests_path))
    errors = number(lookup(fields, errors_path))
    target = number(slo.get("target"))
    if requests is None or errors is None or target is None:
        return None
    if not (0 <= errors <= requests and requests > 0 and 0 < target < 1):
        return None
    if requests != requests.to_integral_value() or errors != errors.to_integral_value():
        return None
    return float(min(25, (errors / requests) / (1 - target)))


def score(severity, environment, count, rule, *, fields=None, schema=None):
    """Optional schema.slo maps requests_total and errors_total paths and a success target."""
    priority = rule.get("priority") or {"critical": "P0", "error": "P1", "warning": "P2"}.get(
        severity, "P3"
    )
    parts = {
        "severity": {"critical": 50, "error": 35, "warning": 20, "info": 5, "debug": 5}.get(
            severity, 0
        ),
        "environment": 25 if environment == "prod" else 0,
        "impact": max(0, min(25, rule.get("impact", 0))),
        "frequency": min(15, 5 * log10(max(1, count))),
    }
    slo = (schema or {}).get("slo")
    burn = _error_budget_burn(slo, fields)
    if burn is not None:
        key = "error_budget_burn_simulated" if slo.get("simulated") is True else "error_budget_burn"
        parts[key] = burn
    return priority, round(sum(parts.values()), 3), parts
