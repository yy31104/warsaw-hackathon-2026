from datetime import UTC, datetime, timedelta

import pytest

from alerting.clock import FixedClock
from alerting.ingest.normalize import normalize
from alerting.rules.engine import Ruleset, evaluate


SOURCE = {"id": "meters", "transport": "http"}
AT = datetime(2026, 1, 15, 12, tzinfo=UTC)
SCHEMA = {"id": "meter", "name": "meter", "detect": {"kind": "reading"}, "severity_default": "warning"}
RULE = {"id": "reading", "route": [{"to": "team:plant", "channels": ["ui"]}]}


def observation(raw, schema, rule, state, at):
    event = normalize(raw, SOURCE, [schema], received_at=at)
    return evaluate(event, Ruleset([rule], [schema]), state, FixedClock(at))


@pytest.mark.parametrize(
    ("rule_options", "schema_options", "expected_group_values"),
    [
        pytest.param(
            {"group": {"by": ["rack", "meter.reading"]}}, {},
            {"rack": "west", "meter.reading": 123.45}, id="group-by",
        ),
        pytest.param({}, {"alert_key": "{{rack}}|{{meter.reading}}"}, {}, id="schema-alert-key"),
    ],
)
def test_scale_change_matches_the_open_alert(rule_options, schema_options, expected_group_values):
    rule = {
        **RULE, **rule_options,
        "match": {"field": "meter.reading", "op": "gt", "value": 100},
        "present": {"fields": ["meter.reading"]},
    }
    schema = {**SCHEMA, **schema_options}
    raw = {"kind": "reading", "rack": "west", "meter": {"reading": 12_345}}
    first_schema = {**schema, "version": 1, "transforms": {"meter.reading": {"scale": 1}}}
    second_schema = {**schema, "version": 2, "transforms": {"meter.reading": {"scale": 0.01}}}

    first = observation(raw, first_schema, rule, {}, AT)
    (created,) = first.alert_ops
    assert first.decisions[0]["outcome"] == "NEW_ALERT"
    assert created.key == "west|12345"
    open_alert = {(rule["id"], created.key): {"id": 7, "status": "open", "severity": "warning",
                                             "last_notified_at": AT}}

    later = observation(raw, second_schema, rule, open_alert, AT + timedelta(minutes=11))
    (updated,) = later.alert_ops
    assert later.decisions[0]["outcome"] == "EXISTING_ALERT"
    assert updated.key == created.key
    assert updated.transition == "cooldown_elapsed"
    assert updated.group_values == expected_group_values
    assert updated.fields == {"meter.reading": 123.45}


@pytest.mark.parametrize(
    ("rule_options", "schema_options"),
    [
        pytest.param({"group": {"by": ["x", "x.raw"]}}, {}, id="group-by"),
        pytest.param({}, {"alert_key": "{{x}}|{{x.raw}}"}, id="schema-alert-key"),
    ],
)
def test_literal_raw_suffix_keeps_its_own_value(rule_options, schema_options):
    schema = {**SCHEMA, **schema_options, "transforms": {"unrelated": {"scale": 0.01}}}
    raw = {"kind": "reading", "x": 21, "x.raw": 72, "unrelated": 250}
    result = observation(raw, schema, {**RULE, **rule_options}, {}, AT)
    (op,) = result.alert_ops
    assert op.key == "21|72"
