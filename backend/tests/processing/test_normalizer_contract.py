"""The DEV2 producer and DEV1 fixture adapter can both pass their output to this assertion."""

import json
from datetime import datetime

from alerting.contracts import CanonicalEvent
from alerting.rules.engine import evaluate

from .test_golden import ROOT, RULESET, START, make_event


def assert_workflow_contract(output, raw):
    event = CanonicalEvent.model_validate(output)
    expected = {
        "$schema": raw["type"],
        "$severity": "error",
        "$source": "workflow-bus",
        "$transport": "rabbitmq",
        "$service": raw["workflowName"],
        "$environment": raw["environment"],
        "$error_code": raw["error"]["code"],
        "$message": raw["error"]["message"],
        "type": raw["type"],
        "severity": raw["severity"],
        "workflowName": raw["workflowName"],
        "workflowId": raw["workflowId"],
        "environment": raw["environment"],
        "step": raw["step"],
        "timestamp": raw["timestamp"],
        "error.code": raw["error"]["code"],
        "error.message": raw["error"]["message"],
    }
    assert {key: event.fields[key] for key in expected} == expected
    assert datetime.fromisoformat(str(event.fields["$time"])) == datetime.fromisoformat(
        raw["timestamp"]
    )
    assert (event.source_id, event.transport, event.schema_id) == (
        "workflow-bus",
        "rabbitmq",
        "workflow",
    )
    assert (event.schema_name, event.severity, event.service, event.environment) == (
        expected["$schema"],
        expected["$severity"],
        expected["$service"],
        expected["$environment"],
    )
    (op,) = evaluate(event, RULESET, {}, START).alert_ops
    assert op.key == "|".join([raw["workflowName"], raw["step"], raw["error"]["code"]])
    assert op.fields["error.message"] == expected["$message"]


def test_workflow_event_contract():
    raw = json.loads((ROOT / "fixtures/events/workflow_failed.json").read_text())
    event = make_event(raw, "workflow", "workflow-bus")
    assert_workflow_contract(event, raw)


def test_severity_map_and_typed_paths():
    raw = json.loads((ROOT / "fixtures/events/workflow_failed.json").read_text())
    raw.update(severity="fAtAl", measurements=[{"ms": 42, "healthy": False}])
    event = make_event(raw, "workflow", "workflow-bus")
    assert event.severity == event.fields["$severity"] == "critical"
    assert event.fields["measurements"] == raw["measurements"]
    assert event.fields["measurements.0.ms"] == 42
    assert event.fields["measurements.0.healthy"] is False
