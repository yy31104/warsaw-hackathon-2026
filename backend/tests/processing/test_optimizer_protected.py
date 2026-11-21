from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from alerting.config import files
from alerting.contracts import CanonicalEvent
from alerting.simulate.history import replay_history
from alerting.simulate.protected import ProtectedScenarios


DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "protected" / "optimizer_demo.yaml"
ORIGIN = datetime.fromisoformat("2026-01-15T21:00:00Z")
CRITICAL = {
    "id": "protected-test",
    "owner": "team:payments",
    "match": {"$severity": "critical"},
    "route": [{"to": "user:anna", "channels": "preferences"}],
    "group": {"by": ["queue.partition"], "cooldown": "5m"},
}


def _event(event_id, at, fields):
    return CanonicalEvent(
        id=event_id, source_id="optimizer-test", transport="http", received_at=at,
        payload_hash=f"test-{event_id}", raw={}, fields=fields, severity=fields["$severity"],
    )


def _check(rule, candidate, directory, events=(), acted_on=()):
    protected = ProtectedScenarios(rule, [], directory, [], acted_on=acted_on)
    baseline = replay_history(events, rule, [], directory, [])
    draft = replay_history(events, candidate, [], directory, [])
    return protected.check(candidate, baseline, draft)


def test_labelled_fixture_allows_cooldown_savings_and_blocks_a_higher_threshold():
    fixture = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    rule, sample = fixture["rule"], fixture["history"]
    directory = files.directory()
    start = datetime.fromisoformat(sample["first_at"])
    events = [
        _event(i + 1, start + timedelta(seconds=i * sample["every_s"]), sample["fields"])
        for i in range(sample["count"])
    ]
    prior = replay_history(events, rule, [], directory, [])
    b = {**rule, "group": {**rule["group"], "cooldown": "30m"}}
    b_replay = replay_history(events, b, [], directory, [])
    protections = ProtectedScenarios(rule, [], directory, [])
    evidence, loss = protections.check(b, prior, b_replay)
    assert evidence["protected_counts"]["labelled_reference"] == 4
    assert evidence["protected_counts"]["safety_boundary"] > 0
    assert loss is None
    assert prior.summary()["notifications"] == {"rita/sms": 18}
    assert b_replay.summary()["notifications"] == {"rita/sms": 4}

    initial, threshold = rule["match"]["all"]
    a = {**rule, "match": {"all": [initial, {**threshold, "value": "312.5"}]}}
    a_replay = replay_history(events, a, [], directory, [])
    _, loss = protections.check(a, prior, a_replay)
    assert a_replay.summary()["notifications"] == {}
    assert loss == {
        "episode_id": "warning-at-threshold", "source": "labelled_reference",
        "note": fixture["episodes"][0]["note"], "recipient": "rita", "channel": "sms",
        "kind": "missing",
    }


@pytest.mark.parametrize(("every", "delay"), [("60s", None), ("61s", 61)])
def test_labelled_maximum_added_delay_is_inclusive(every, delay):
    fixture = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    rule = fixture["rule"]
    draft = {**rule, "group": {**rule["group"], "mode": "digest", "every": every}}
    _, loss = _check(rule, draft, files.directory())
    if delay is None:
        assert loss is None
    else:
        assert loss["source"] == "labelled_reference"
        assert loss["kind"] == "delayed"
        assert loss["delay_s"] == delay


def test_boundary_threshold_applies_to_warning_rules_without_labels():
    rule = {
        **CRITICAL,
        "route": [{"to": "user:anna", "channels": ["sms"]}],
        "match": {"all": [
            {"$severity": "warning"},
            {"field": "queue.age_ms", "op": "gte", "value": 250},
        ]},
    }
    first, edge = rule["match"]["all"]
    draft = {**rule, "match": {"all": [first, {**edge, "value": 251}]}}
    evidence, loss = _check(rule, draft, files.directory())
    assert evidence["protected_counts"]["labelled_reference"] == 0
    assert loss["source"] == "safety_boundary"
    assert loss["recipient"] == "anna" and loss["channel"] == "sms"


def test_boundary_keeps_the_night_preference_pair_and_missing_contact_fallback():
    directory = files.directory()
    sms_only = {**CRITICAL, "route": [{"to": "user:anna", "channels": ["sms"]}]}
    _, loss = _check(CRITICAL, sms_only, directory)
    assert loss["source"] == "safety_boundary" and loss["channel"] == "email"
    assert loss["scenario"] == {
        "local_time": "2026-01-15T07:59:00+01:00", "timezone": "Europe/Warsaw", "missing_contact": None,
    }

    bypass_fallback = {**CRITICAL, "route": [*CRITICAL["route"], {"to": "user:ops", "channels": ["ui"]}]}
    _, loss = _check(CRITICAL, bypass_fallback, directory)
    assert loss["source"] == "safety_boundary" and loss["kind"] == "missing"
    assert loss["recipient"] == "team:platform" and loss["channel"] == "ui"
    assert loss["scenario"]["missing_contact"] == "anna"


def test_critical_history_keeps_pairs_from_declared_and_team_required_channels():
    directory = files.directory()
    payments = next(team for team in directory["teams"] if team["id"] == "payments")
    payments["required_channels"] = {"critical": ["email"]}
    rule = {**CRITICAL, "route": [*CRITICAL["route"], {"to": "user:paul", "channels": ["phone"]}]}
    events = [_event(42, ORIGIN, {"$severity": "critical", "queue.partition": "orders"})]

    _, loss = _check(rule, {**rule, "route": CRITICAL["route"]}, directory, events)
    assert loss == {
        "source": "critical_channels", "event_id": 42,
        "recipient": "paul", "channel": "phone", "kind": "missing",
    }

    draft = {**rule, "route": [{"to": "user:anna", "channels": ["sms"]}, rule["route"][1]]}
    _, loss = _check(rule, draft, directory, events)
    assert loss == {
        "source": "critical_channels", "event_id": 42,
        "recipient": "anna", "channel": "email", "kind": "missing",
    }


def test_required_critical_channel_with_missing_baseline_records_the_limitation():
    directory = files.directory()
    rule = {**CRITICAL, "route": [{"to": "user:anna", "channels": ["sms"]}]}
    evidence, loss = _check(rule, rule, directory)
    assert loss is None
    assert any(
        row["channel"] == "sms" and row["scenario"]["missing_contact"] == "anna"
        for row in evidence["baseline_limitations"]
    )

    payments = next(team for team in directory["teams"] if team["id"] == "payments")
    payments["required_channels"] = {"critical": ["email"]}
    evidence, loss = _check(rule, rule, directory)
    assert loss["source"] == "critical_channels" and loss["channel"] == "email"
    assert loss["recipient"] == "anna" and loss["scenario"]["missing_contact"] is None


def test_newly_matching_critical_boundary_honors_an_identifiable_declared_pair():
    directory = files.directory()
    anna = next(user for user in directory["users"] if user["id"] == "anna")
    anna["preferences"] = [
        {"min_severity": "critical", "channels": ["email"]},
        {"min_severity": "warning", "channels": ["sms"]},
    ]
    rule = {
        **CRITICAL, "match": {"$severity": "warning"},
        "route": [{"to": "user:anna", "channels": ["sms"]}],
    }
    draft = {
        **CRITICAL, "match": {"field": "$severity", "op": "in", "value": ["warning", "critical"]},
    }
    evidence, loss = _check(rule, draft, directory)
    assert evidence["protected_counts"]["critical_channels"] == 1
    assert loss["episode_id"] == "new-critical-boundary-1"
    assert loss["source"] == "critical_channels" and loss["kind"] == "missing"
    assert loss["recipient"] == "anna" and loss["channel"] == "sms"


def test_acted_on_incident_loss_is_reported_as_evidence_without_rejecting():
    directory = files.directory()
    fields = {"$severity": "critical", "queue.partition": "orders"}
    events = [_event(1, ORIGIN, fields), _event(2, ORIGIN + timedelta(minutes=6), fields)]
    candidate = {**CRITICAL, "group": {**CRITICAL["group"], "cooldown": "30m"}}
    evidence, loss = _check(CRITICAL, candidate, directory, events, [
        {"alert_id": 21, "event_id": 1}, {"alert_id": 21, "event_id": 2},
    ])
    assert loss is None
    assert evidence["acted_on_incidents_checked"] == 1
    assert evidence["acted_on_incidents_affected"] == [{
        "alert_id": 21, "event_id": 2, "recipient": "anna", "channel": "email", "kind": "missing",
    }]


def test_acted_on_evidence_reports_short_delays_earlier_plans_and_additions():
    directory = files.directory()
    fields = {"$severity": "critical", "queue.partition": "orders"}
    events = [
        _event(1, ORIGIN, fields),
        _event(2, ORIGIN + timedelta(minutes=1), {**fields, "$severity": "warning"}),
    ]
    acted_on = [{"alert_id": 21, "event_id": 1}]
    digest = {**CRITICAL, "group": {**CRITICAL["group"], "mode": "digest", "every": "30s"}}
    base = {"alert_id": 21, "event_id": 1, "recipient": "anna", "channel": "email"}

    evidence, loss = _check(CRITICAL, digest, directory, events, acted_on)
    assert loss is None
    assert evidence["acted_on_incidents_affected"] == [{**base, "kind": "delayed", "delay_s": 30}]

    evidence, loss = _check(digest, CRITICAL, directory, events, acted_on)
    assert loss is None
    assert evidence["acted_on_incidents_affected"] == [{**base, "kind": "earlier", "delay_s": -30}]

    extra_sms = {**CRITICAL, "route": [*CRITICAL["route"], {"to": "user:anna", "channels": ["sms"]}]}
    evidence, loss = _check(CRITICAL, extra_sms, directory, events, acted_on)
    assert loss is None
    assert evidence["acted_on_incidents_affected"] == [{**base, "channel": "sms", "kind": "added"}]
