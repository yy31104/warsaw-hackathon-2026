from datetime import timedelta

import pytest

from alerting.clock import FixedClock
from alerting.contracts import Severity
from alerting.rules.engine import Ruleset, evaluate
from alerting.rules.silences import Silence

from .test_late_events import CLOCK, RULE, RULESET, SCHEMA, event


def window(include_critical=True):
    return Silence.from_row({
        "id": 7, "team_id": "plant", "created_by": "alex", "include_critical": include_critical,
        "match": {"rack": "west"}, "created_at": CLOCK.now() - timedelta(hours=1),
        "expires_at": CLOCK.now() + timedelta(hours=1),
    })


class Maintenance(dict):
    def __init__(self, silence, current=()):
        super().__init__(current)
        self.silence = silence

    def get_silences(self, rule_id):
        return (self.silence,) if rule_id == RULE["id"] else ()


@pytest.mark.parametrize("mode", ["immediate", "digest"])
def test_silenced_match_keeps_its_count_operation_and_explanation(mode):
    silence = window()
    rule = {**RULE, "group": {**RULE["group"], "mode": mode}}
    ruleset = Ruleset([rule, {**RULE, "id": "another-owner"}], [SCHEMA])
    result = evaluate(event(), ruleset, Maintenance(silence), CLOCK)
    silenced, unaffected = result.alert_ops
    assert silenced.transition == "created" and not silenced.notify
    assert unaffected.transition == "created" and unaffected.notify
    decision, other = result.decisions
    assert (decision["outcome"], other["outcome"]) == ("SILENCED", "NEW_ALERT")
    assert decision["reason"]["silence"] == {
        "id": 7, "created_by": "alex", "expires_at": silence.expires_at.isoformat(),
    }
    assert decision["reason"]["message"] == f"Silenced by alex until {silence.expires_at.isoformat()}"


def test_critical_page_outside_match_and_after_expiry():
    silence = window()
    state = Maintenance(silence)
    for observation, clock in [(event(rack="east"), CLOCK), (event(), FixedClock(silence.expires_at))]:
        result = evaluate(observation, RULESET, state, clock)
        assert result.decisions[0]["outcome"] == "NEW_ALERT" and result.alert_ops[0].notify


def test_critical_alert_keeps_critical_exemption_on_lower_severity_followup():
    current = {("pressure", "west"): {
        "id": 15, "severity": "critical", "status": "open",
        "last_notified_at": CLOCK.now() - timedelta(hours=1), "latest_episode": "open-cycle",
    }}
    state = Maintenance(window(include_critical=False), current)
    warning = event().model_copy(update={"severity": Severity.warning})
    result = evaluate(warning, RULESET, state, CLOCK)
    assert result.decisions[0]["outcome"] == "EXISTING_ALERT" and result.alert_ops[0].notify

    resolved = evaluate(event(phase="clear", cycle="open-cycle"), RULESET, state, CLOCK)
    assert resolved.decisions[0]["outcome"] == "RESOLVED"
