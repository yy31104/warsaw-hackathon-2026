"""{{path}} template rendering. SHARED FILE (used by rules, routing, runbooks). CONTRACT §3.5.

Substitution only — no expressions, no code execution. Missing/None -> ‹missing: path›.
"""
from __future__ import annotations

import re
from typing import Any

_VAR = re.compile(r"\{\{\s*([$\w][\w.\-$]*)\s*\}\}")


def lookup(fields: dict[str, Any], path: str) -> Any:
    """Flattened lookup first (fields["payload.currency"]), then nested walk over raw dicts."""
    if path in fields:
        return fields[path]
    node: Any = fields
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def render(template: str | None, fields: dict[str, Any]) -> str:
    if not template:
        return ""

    def sub(match: re.Match[str]) -> str:
        value = lookup(fields, match.group(1))
        return f"‹missing: {match.group(1)}›" if value is None else str(value)

    return _VAR.sub(sub, template)


def variables(template: str | None) -> list[str]:
    return _VAR.findall(template or "")
