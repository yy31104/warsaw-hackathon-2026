from datetime import datetime

import pytest
from alerting.clock import FixedClock
from alerting.config import files
from alerting.contracts import AlertSnapshot
from alerting.routing import plan

from .test_golden import CASES, RULESET, START, case_event, evaluate


@pytest.mark.parametrize(
    "case", [c for c in CASES if "notifications" in c["expect"]], ids=lambda c: c["id"]
)
def test_golden_channels(case):
    event = case_event(case)
    when = case["send"].get("headers", {}).get("X-Demo-Clock")
    clock = FixedClock(datetime.fromisoformat(when)) if when else START
    (op,) = evaluate(event, RULESET, {}, clock).alert_ops
    alert = AlertSnapshot(
        id=19,
        rule_id=op.rule["id"],
        rule_version=1,
        alert_key=op.key,
        status="open",
        severity=event.severity,
        title=op.title,
        body=op.body,
    )
    result = plan(
        alert, op.transition, op.rule, event.fields, files.directory(), files.runbooks(), {}, clock
    )
    assert {(i.recipient, i.channel.value) for i in result.intents} == {
        (n["recipient"], n["channel"]) for n in case["expect"]["notifications"]
    }
    for intent in result.intents:
        assert "http://localhost:5173/a/19" in intent.rendered.short
        assert len(intent.rendered.short) <= 160
    if "runbook" in case["expect"].get("alert", {}):
        assert result.runbook_id == case["expect"]["alert"]["runbook"]
