from math import log10


def score(severity, environment, count, rule):
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
    return priority, round(sum(parts.values()), 3), parts
