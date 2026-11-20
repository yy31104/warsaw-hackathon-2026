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
from alerting.contracts import CanonicalEvent
from alerting.rules.engine import Ruleset, evaluate
from alerting.templates import lookup, render

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


def make_event(raw, schema_id):
    schema = next(s for s in files.schemas() if s["id"] == schema_id)
    fields = dict(raw)
    for alias, path in schema.get("aliases", {}).items():
        fields["$" + alias] = lookup(raw, path)
    severity = str(fields.get("$severity") or schema.get("severity_default", "unknown"))
    fields["$severity"] = schema.get("severity_map", {}).get(severity, severity)
    fields["$schema"] = render(schema["name"], fields)
    return CanonicalEvent(id=1, source_id="sample", transport="http", payload_hash="hash",
                          received_at=START.now(), schema_id=schema_id, schema_name=fields["$schema"],
                          severity=fields["$severity"], raw=raw, fields=fields)


CASES = [c for c in GOLD["cases"] if c["id"].startswith(("settlements-", "rates-", "anna-")) and "send" in c]


def case_event(case):
    send = case["send"]
    raw = json.loads((ROOT / send["file"]).read_text())
    merge(raw, send.get("mutate", {}))
    return make_event(raw, {"workflow-bus": "workflow", "trade-bus": "business-envelope",
                            "alertmanager": "alertmanager"}[send["source"]])


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
        event = make_event(dict(level=level, instance=instance, instrument=instrument), "pricing-log")
        for op in evaluate(event, RULESET, {}, START).alert_ops:
            assert not op.notify
            counts[op.key] += 1
    assert counts == {"EURUSD": 3, "USDJPY": 1}
