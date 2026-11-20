from copy import deepcopy

import pytest

from alerting.rules.safety import check
from alerting.rules.storage import Settings
from .test_golden import GOLD


def test_golden_counterexample_and_ui_only_digest():
    settings = Settings.load()
    cases = {case["id"]: case for case in GOLD["cases"]}
    ghost = cases["rule-safety-counterexample"]
    report = check(ghost["body"]["rule"], settings.ruleset, [], settings.directory)
    assert len(report["scenarios_checked"]) >= ghost["expect"]["scenarios_checked_gte"]
    assert any(
        ghost["expect"]["violation_problem_contains"] in violation["problem"]
        and violation["trace"][0]["field"] == "$severity"
        for violation in report["violations"]
    )

    ui = cases["rule-safety-ui-only-is-valid"]
    rule = next(rule for rule, _ in settings.ruleset.rules if rule["id"] == ui["body"]["rule_id"])
    report = check(rule, settings.ruleset, [], settings.directory)
    assert len(report["violations"]) == ui["expect"]["violations"]
    assert any(scenario["matched"] for scenario in report["scenarios_checked"])


def test_declared_channels_each_require_a_destination_and_policy_uses_the_owner():
    settings = Settings.load()
    directory = deepcopy(settings.directory)
    next(user for user in directory["users"] if user["id"] == "sam")["contacts"].pop("phone")
    next(team for team in directory["teams"] if team["id"] == "settlements")["required_channels"] = {
        "critical": ["phone"],
    }
    rule = {
        "owner": "user:sam",
        "match": {"field": "size", "op": "gt", "value": 10},
        "route": [
            {"to": "oncall:settlements", "channels": ["phone", "sms"]},
            {"to": "team:settlements", "channels": ["ui"]},
        ],
    }
    schemas = [{"id": "sample", "environments": ["east", "west"], "samples": [{"fields": {"size": 9}}]}]
    before = deepcopy((rule, schemas, directory))
    report = check(rule, settings.ruleset, schemas, directory)
    assert (rule, schemas, directory) == before
    assert {"problem": "never fires", "samples_checked": 1} in report["warnings"]
    assert {s["fields"]["size"] for s in report["scenarios_checked"]} >= {9, 10, 11}
    matches = [s for s in report["scenarios_checked"] if s["matched"]]
    assert {s["fields"]["$environment"] for s in matches} == {"east", "west"}
    assert {s["local_time"][11:16] for s in matches} >= {"07:59", "08:00", "19:59", "20:00", "23:10"}
    assert any(s["missing_contact"] == "sam" for s in matches)
    violations = [v for v in report["violations"] if v["scenario"]["missing_contact"] is None]
    assert {v["requirement"] for v in violations} == {"explicit channels", "team required_channels"}
    assert all("phone" in v["problem"] for v in violations)
    assert all(v["scenario"]["fields"]["$severity"] == "critical"
               for v in violations if v["requirement"] == "team required_channels")


def test_target_validation_also_applies_when_no_scenario_can_match():
    settings = Settings.load()
    rule = {
        "match": {"all": [{"kind": "a"}, {"kind": "b"}]},
        "route": [{"to": "user:absent", "channels": ["ui"]}],
    }
    report = check(rule, settings.ruleset, [], settings.directory)
    assert not any(s["matched"] for s in report["scenarios_checked"])
    assert any(v["requirement"] == "route target" for v in report["violations"])


def test_team_policy_checks_the_combined_plan_and_explicit_channels_check_the_target():
    settings = Settings.load()
    directory = deepcopy(settings.directory)
    next(t for t in directory["teams"] if t["id"] == "settlements")["required_channels"] = {
        "critical": ["ui"]
    }
    next(u for u in directory["users"] if u["id"] == "sam")["contacts"].clear()
    rule = {
        "owner": "team:settlements",
        "match": {"$severity": "critical"},
        "route": [
            {"to": "user:sam", "channels": ["email"]},
            {"to": "user:lee", "channels": ["email"]},
        ],
    }
    report = check(rule, settings.ruleset, [], directory)
    present = [v for v in report["violations"] if v["scenario"]["missing_contact"] is None]
    assert {v["problem"] for v in present} == {
        "user:sam has no usable email destination", "settlements requires ui"
    }


def test_critical_preferences_accept_ui_fallback_and_report_an_empty_plan():
    settings = Settings.load()
    directory = deepcopy(settings.directory)
    rule = {"match": {"$severity": "critical"}, "route": [{"to": "user:anna", "channels": "preferences"}]}
    schemas = [{"id": "test", "samples": [{"$severity": "critical"}]}]
    assert not check(rule, settings.ruleset, schemas, directory)["violations"]
    directory.pop("fallback_team")
    report = check(rule, settings.ruleset, schemas, directory)
    violations = report["violations"]
    assert violations
    assert all(v["requirement"] == "critical preferences" and v["scenario"]["missing_contact"] == "anna"
               for v in violations)
    assert not report["warnings"]


@pytest.mark.parametrize("pattern", ["x" * 201, "(item+)+"])
def test_expensive_regex_is_reported_without_running_it(pattern):
    settings = Settings.load()
    rule = {"match": {"field": "name", "op": "regex", "value": pattern}}
    report = check(rule, settings.ruleset, [], settings.directory)
    assert report["warnings"] == [{"problem": "regex too long or complex", "field": "name"}]
    assert report["scenarios_checked"] == []
