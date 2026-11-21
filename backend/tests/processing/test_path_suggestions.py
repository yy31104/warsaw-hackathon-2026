import pytest

from alerting.rules import suggestions
from alerting.rules.suggestions import sample_filters, suggest_paths, unknown_fields


@pytest.mark.parametrize(
    ("path", "suggestion"),
    [
        ("errorCode", "error.code"),
        ("ERROR_CODE", "error.code"),
        ("error-code", "error.code"),
        ("Notional", "notional"),
        ("notinoal", "notional"),
        ("$Severity", "$severity"),
    ],
)
def test_suggest_paths_returns_original_spelling(path, suggestion):
    assert suggest_paths(path, ["room", "$severity", "notional", "error.code"]) == [suggestion]


def test_suggest_paths_omits_exact_or_unrelated_names():
    known = ["notional", "error.code", "$severity"]
    assert suggest_paths("notional", known) == []
    assert suggest_paths("xyz", known) == []


def test_suggestions_are_unique_bounded_and_independent_of_input_order():
    known = ["Error_Code", "error.code", "error-code", "error_code", "error_code"]
    ranked = suggest_paths("errorCode", known)
    assert len(ranked) == len(set(ranked)) == 3
    assert ranked == suggest_paths("errorCode", reversed(known))
    assert suggest_paths("errorCode", known, limit=1) == ranked[:1]
    assert suggest_paths("errorCode", known, limit=0) == []


def test_canonical_fields_require_no_event_history():
    rule = {"match": {"$severity": "critical"}, "present": {"title": "{{$service}}"}}
    assert sample_filters(rule) == []


def test_field_advice_does_not_run_authored_regexes_against_metadata(monkeypatch):
    rule = {"match": {"field": "$schema", "op": "regex", "value": "sample.*"},
            "present": {"fields": ["Notional"]}}
    samples = [("demo", "sample", "test", "http", ["notional"])]

    def unexpected_regex(*args):
        pytest.fail("Field advice should not execute the author's regex on stored names")

    monkeypatch.setattr(suggestions, "compile_match", unexpected_regex)
    assert unknown_fields(rule, [], samples)[0]["suggestions"] == ["notional"]
