"""Compile the shared configuration language once, preserving every leaf in its trace."""
import fnmatch
import operator
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from alerting.contracts import SEVERITY_ORDER
from alerting.templates import lookup

Predicate = Callable[[dict], tuple[bool, list[dict]]]
ORDER = {"lt": operator.lt, "lte": operator.le, "gt": operator.gt, "gte": operator.ge}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def equal(left, right):
    a, b = number(left), number(right)
    if a is not None and b is not None:
        return a == b
    return type(left) is type(right) and left == right


def compile(match: dict) -> Predicate:
    if not isinstance(match, dict):
        raise ValueError("match must be an object")
    if "all" in match or "any" in match:
        key = "all" if "all" in match else "any"
        if set(match) != {key} or not isinstance(match[key], list):
            raise ValueError("invalid logical group")
        children = [compile(child) for child in match[key]]
        reduce = all if key == "all" else any

        def group(fields):
            results = [child(fields) for child in children]
            return reduce(ok for ok, _ in results), [row for _, rows in results for row in rows]
        return group
    if "not" in match:
        if len(match) != 1:
            raise ValueError("invalid negation")
        child = compile(match["not"])

        def negate(fields):
            ok, trace = child(fields)
            return not ok, trace
        return negate
    if "field" not in match and "op" not in match:
        return compile({"all": [{"field": k, "op": "eq", "value": v} for k, v in match.items()]})
    path, op, expected = match.get("field"), match.get("op"), match.get("value")
    if not isinstance(path, str) or not path or op not in {*ORDER, "eq", "neq", "in", "not_in", "contains", "glob", "regex", "exists", "not_exists"}:
        raise ValueError("invalid predicate field or operator")
    if op not in ("exists", "not_exists") and "value" not in match:
        raise ValueError("missing comparison value")
    if op in ("in", "not_in") and not isinstance(expected, list):
        raise ValueError("membership requires a list")
    if op in ("regex", "glob") and not isinstance(expected, str):
        raise ValueError("pattern must be a string")
    regex = None
    if op == "regex":
        if len(expected) > 200:
            raise ValueError("regex exceeds 200 characters")
        try:
            regex = re.compile(expected)
        except re.error as exc:
            raise ValueError("invalid regular expression") from exc

    def compare(actual):
        if op in ("exists", "not_exists"):
            return (actual is not None) == (op == "exists")
        if actual is None or (path == "$severity" and actual not in SEVERITY_ORDER):
            return False
        if op in ORDER:
            rank = SEVERITY_ORDER.get if path == "$severity" else number
            a, b = rank(actual), rank(expected)
            return a is not None and b is not None and ORDER[op](a, b)
        if op in ("eq", "neq"):
            return equal(actual, expected) == (op == "eq")
        if op in ("in", "not_in"):
            return any(equal(actual, x) for x in expected) == (op == "in")
        if op == "contains":
            if isinstance(actual, list):
                return any(equal(x, expected) for x in actual)
            return isinstance(actual, (str, dict)) and isinstance(expected, str) and expected in actual
        if not isinstance(actual, str):
            return False
        if op == "glob":
            return fnmatch.fnmatchcase(actual, expected)
        return len(actual.encode("utf-8")) <= 4096 and regex.search(actual) is not None

    def predicate(fields):
        actual = lookup(fields, path)
        ok = compare(actual)
        return ok, [{"field": path, "op": op, "value": expected, "actual": actual, "matched": ok}]
    return predicate
