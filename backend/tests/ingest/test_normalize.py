import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alerting.config import files
from alerting.ingest.normalize import InvalidEvent, flatten, normalize, parse_log_line


FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "events"
RECEIVED = datetime(2026, 10, 23, 10, tzinfo=UTC)
SOURCES = {s["id"]: s for s in files.sources()}
SCHEMAS = files.schemas()


def prepare(raw, source="generic-webhook", **kwargs):
    return normalize(raw, SOURCES[source], SCHEMAS, received_at=RECEIVED, **kwargs)


@pytest.mark.parametrize("file,source,schema,severity,service,error", [
    ("workflow_failed.json", "workflow-bus", "WORKFLOW_FAILED", "error", "order-settlement", "TIMEOUT"),
    ("alertmanager_high_error_rate.json", "alertmanager", "alertmanager", "critical", "payments-api", "HighErrorRate"),
    ("trade_rejected_v2.json", "trade-bus", "trade.rejected.v2", "warning", "trade-gateway", "LIMIT_BREACH"),
])
def test_repository_examples(file, source, schema, severity, service, error):
    raw = json.loads((FIXTURES / file).read_text(encoding="utf-8"))
    event = prepare(raw, source)
    assert event.status == "pending" and event.schema_version == 1
    assert event.fields["$schema"] == event.schema_name == schema
    assert event.fields["$severity"] == event.severity == severity
    assert event.fields["$service"] == event.service == service
    assert event.fields["$error_code"] == error
    assert event.fields["$transport"] == SOURCES[source]["transport"]
    assert event.fields["$source"] == source
    assert event.raw == raw and event.occurred_at is not None
    assert event.lane == ("critical" if severity == "critical" else "normal")
    if source == "workflow-bus":
        assert event.fields["error.code"] == "TIMEOUT" and event.fields["$environment"] == "prod"
        assert event.source_event_id == "WF-20261023-00042|validate-counterparty|2026-10-23T09:14:02.331Z"
    if source == "trade-bus":
        assert event.fields["payload.notional"] == 25000000
        assert event.occurred_at.timestamp() == raw["meta"]["ts"] / 1000


def test_pricing_log_file():
    source = SOURCES["pricing-logs"]
    lines = (FIXTURES / "pricing-engine.log").read_text(encoding="utf-8").splitlines()
    events = [prepare(parse_log_line(line, source["parser"]), source["id"]) for line in lines]
    assert [e.severity.value for e in events] == ["info", "warning", "warning", "error", "info", "warning", "warning"]
    assert all(e.schema_name == "log.pricing" and e.source_event_id is None for e in events)
    assert events[1].fields["$message"] == "price latency above 1s"
    assert events[1].service == "pricing-engine-2" and events[1].fields["instrument"] == "EURUSD"
    with pytest.raises(InvalidEvent):
        parse_log_line("broken line", source["parser"])


def test_unknown_shape_is_preserved_and_can_be_onboarded_with_config_only():
    raw = {"kind": "sensor.high", "readings": [{"value": 42}], "at": 1, "$severity": "critical"}
    pending = prepare(raw)
    assert pending.raw == raw and pending.status == "needs_review" and pending.schema_id is None
    assert pending.fields["readings"] == raw["readings"] and pending.fields["readings.0.value"] == 42
    assert pending.fields["$severity"] == "unknown" and pending.lane == "normal"
    schema = {"id": "sensor", "version": 3, "name": "{{kind}}", "detect": {"field": "kind", "op": "exists"},
              "aliases": {"time": "at"}, "time_format": "epoch_s", "severity_default": "info"}
    event = normalize(raw, SOURCES["generic-webhook"], [schema, dict(schema, id="later")], received_at=RECEIVED)
    assert (event.schema_id, event.schema_version, event.schema_name) == ("sensor", 3, "sensor.high")
    assert event.status == "pending" and event.severity == "info" and event.occurred_at.timestamp() == 1


def test_identity_precedence_and_canonical_hash():
    raw = json.loads((FIXTURES / "workflow_failed.json").read_text(encoding="utf-8"))
    reordered = dict(reversed(raw.items()))
    reordered["error"] = dict(reversed(raw["error"].items()))
    a = prepare(raw, "workflow-bus", message_id="transport-1")
    b = prepare(reordered, "workflow-bus", message_id="transport-2")
    assert a.source_event_id == "transport-1" and b.source_event_id == "transport-2"
    assert len(a.payload_hash) == 64 and a.payload_hash == b.payload_hash
    assert prepare(dict(raw, severity="FATAL"), "workflow-bus").payload_hash != a.payload_hash
    assert prepare(dict(raw, step=None), "workflow-bus").source_event_id is None
    assert prepare({"x": 1}).source_event_id is None
    event = normalize({"x": 1}, dict(SOURCES["generic-webhook"], dedup="payload_hash"), [], received_at=RECEIVED)
    assert event.source_event_id == event.payload_hash


def test_severity_mapping_is_case_tolerant_and_bad_time_stays_queryable():
    raw = json.loads((FIXTURES / "workflow_failed.json").read_text(encoding="utf-8"))
    event = prepare(dict(raw, severity="eRrOr", timestamp="not-a-date"), "workflow-bus")
    assert event.severity == "error" and event.occurred_at is None
    assert event.fields["timestamp"] == "not-a-date"
    assert prepare(dict(raw, severity="novel"), "workflow-bus").severity == "unknown"


def test_configured_alias_identity_long_keys_and_string_epoch():
    schema = {"id": "custom", "name": "reading", "detect": {"field": "sensor", "op": "exists"},
              "aliases": {"service": "sensor", "time": "ts"}, "time_format": "epoch_ms",
              "event_id": "{{$service}}|{{$time}}"}
    raw = {"sensor": 13, "ts": "1792833245000"}
    event = normalize(raw, SOURCES["generic-webhook"], [schema], received_at=RECEIVED)
    assert event.service == event.fields["$service"] == "13"
    assert event.raw["ts"] == raw["ts"] and event.occurred_at.timestamp() == 1792833245
    assert event.source_event_id == "13|" + event.occurred_at.isoformat()
    long_id = "ABCDE" * 2000
    compact = prepare(raw, message_id=long_id).source_event_id
    assert compact == prepare(raw, message_id=long_id).source_event_id
    assert compact != prepare(raw, message_id=compact).source_event_id and len(compact) < 100


def test_secrets_are_masked_in_both_views_but_conflicting_bodies_still_differ():
    a = prepare({"items": [{"api_key": "first-script-key"}], "email": "demo@example.invalid"})
    b = prepare({"items": [{"api_key": "different-script-key"}], "email": "demo@example.invalid"})
    for view in (a.raw, a.fields):
        assert "first-script-key" not in json.dumps(view) and "demo@example.invalid" not in json.dumps(view)
    assert a.payload_hash != b.payload_hash


def test_contract_limits_and_native_types():
    assert flatten({"arr": [False, 0, None]}) == {"arr": [False, 0, None], "arr.0": False, "arr.1": 0, "arr.2": None}
    assert len(flatten({str(i): i for i in range(2000)})) == 2000
    with pytest.raises(InvalidEvent, match="2000"):
        prepare({str(i): i for i in range(2001)})
    deep = 1
    for _ in range(20):
        deep = {"x": deep}
    prepare(deep)
    with pytest.raises(InvalidEvent, match="depth"):
        prepare({"extra": deep})
    with pytest.raises(InvalidEvent, match="256"):
        prepare({"text": "x" * 262144})
    with pytest.raises(InvalidEvent):
        prepare({"text": "\0"})
