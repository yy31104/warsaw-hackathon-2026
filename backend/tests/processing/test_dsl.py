import pytest
from alerting.rules.dsl import compile


@pytest.mark.parametrize("op,value,ok", [
    ("eq", 5, True), ("neq", 5, False), ("in", [5], True), ("not_in", [6], True),
    ("gte", 5, True), ("gt", 5, False), ("lt", 6, True), ("lte", 4, False),
    ("glob", "0*", True), ("regex", "^0", True), ("contains", "5", True),
    ("exists", None, True), ("not_exists", None, False),
])
def test_operators(op, value, ok):
    result, trace = compile({"field": "x", "op": op, "value": value})({"x": "05"})
    assert result is ok and trace[0]["actual"] == "05"


def test_tree_and_missing():
    predicate = compile({"all": [{"any": [{"x": 2}, {"y": 3}]}, {"not": {"z": 0}}]})
    ok, trace = predicate({"x": "2"})
    assert ok and len(trace) == 3
    for op in ("exists", "neq", "not_in"):
        assert not compile({"field": "missing", "op": op, "value": []})({})[0]
    assert compile({"field": "missing", "op": "not_exists"})({})[0]


def test_severity_and_limits():
    pred = compile({"field": "$severity", "op": "gte", "value": "error"})
    assert pred({"$severity": "critical"})[0]
    assert not pred({"$severity": "unknown"})[0]
    with pytest.raises(ValueError):
        compile({"field": "x", "op": "regex", "value": "a" * 201})
