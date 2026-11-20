"""Replay the published cases at the canonical-event boundary."""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from alerting.clock import FixedClock
from alerting.config import files
from alerting.ingest.normalize import normalize
from alerting.rules.engine import Ruleset, evaluate

ROOT = Path(__file__).resolve().parents[3]
GOLD = yaml.safe_load((ROOT / "fixtures/golden/expectations.yaml").read_text())
START = FixedClock(datetime.fromisoformat(GOLD["clock_default"]))
RULESET = Ruleset(files.rules(), files.schemas())


def merge(raw, changes):
    for k, v in changes.items():
        if isinstance(v, dict):
            merge(raw[k], v)
        else:
            raw[k] = v


def make_event(raw, schema_id, source_id):
    source = next(s for s in files.sources() if s["id"] == source_id)
    event = normalize(raw, source, files.schemas(), received_at=START.now())
    assert event.schema_id == schema_id
    return event


CASES = [
    c
    for c in GOLD["cases"]
    if c["id"].startswith(("settlements-", "rates-", "anna-")) and "send" in c
]


def case_event(case):
    send = case["send"]
    raw = json.loads((ROOT / send["file"]).read_text())
    merge(raw, send.get("mutate", {}))
    return make_event(
        raw,
        {
            "workflow-bus": "workflow",
            "trade-bus": "business-envelope",
            "alertmanager": "alertmanager",
        }[send["source"]],
        send["source"],
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_match(case):
    result = evaluate(case_event(case), RULESET, {}, START)
    expect = case["expect"]
    if "no_alert_for_rule" in expect:
        assert expect["no_alert_for_rule"] not in [op.rule["id"] for op in result.alert_ops]
        return
    assert len(result.alert_ops) == 1
    op = result.alert_ops[0]
    alert = expect.get("alert", {})
    assert op.transition == "created"
    if "rule" in alert:
        assert op.rule["id"] == alert["rule"]
    if "title_equals" in alert:
        assert op.title == alert["title_equals"]
    for text in alert.get("title_contains", []):
        assert text in op.title


def test_pricing_digest():
    counts = Counter()
    for line in (ROOT / "fixtures/events/pricing-engine.log").read_text().splitlines():
        _, level, instance, rest = line.split(maxsplit=3)
        instrument = re.search("instrument=(\\w+)", rest)[1]
        event = make_event(
            dict(level=level, instance=instance, instrument=instrument),
            "pricing-log",
            "pricing-logs",
        )
        for op in evaluate(event, RULESET, {}, START).alert_ops:
            assert not op.notify
            counts[op.key] += 1
    assert counts == {"EURUSD": 3, "USDJPY": 1}


def test_alertmanager_auto_resolve_without_firing_predicate():
    case = next(c for c in CASES if c["id"] == "anna-daytime-sms")
    event = case_event(case)
    (op,) = evaluate(event, RULESET, {}, START).alert_ops
    event.fields.update(status="resolved", **{"$severity": "info"})
    state = {
        (op.rule["id"], op.key): {
            "status": "open",
            "severity": "critical",
            "latest_episode": event.fields["startsAt"],
        }
    }
    result = evaluate(event, RULESET, state, START)
    (resolved,) = result.alert_ops
    assert resolved.transition == "resolved" and not resolved.notify
