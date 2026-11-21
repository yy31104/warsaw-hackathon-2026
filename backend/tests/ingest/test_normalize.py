import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alerting.config import files
from alerting.ingest.normalize import InvalidEvent, flatten, normalize, parse_log_line, pre_transform_fields


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


def test_pre_transform_fields_only_restores_configured_paths_with_raw_companions():
    fields = {"data.sequence": 12, "data.sequence.raw": "001200", "zero": 42, "zero.raw": 0,
              "other": "keep", "other.raw": "producer supplied", "unscaled": "text"}
    transforms = {path: {"scale": 0.01} for path in ("data.sequence", "zero", "unscaled", "absent")}
    view = pre_transform_fields(fields, transforms)
    assert view == {**fields, "data.sequence": "001200", "zero": 0}
    assert fields["data.sequence"] == 12 and fields["zero"] == 42
    assert pre_transform_fields(fields, None) == pre_transform_fields(fields, {}) == fields


def test_scaled_aliases_are_stable_in_keys_and_converted_in_display_fields():
    raw = {"kind": "tick", "sequence": 1200}
    schema = {"id": "tick", "name": "tick.{{sequence}}", "detect": {"kind": "tick"},
              "aliases": {"service": "sequence", "severity": "sequence", "time": "sequence"},
              "severity_map": {"1200": "warning", "12": "critical"}, "time_format": "epoch_s",
              "episode_key": "{{$service}}|{{$time}}", "event_id": "{{$episode}}|{{$schema}}|{{$severity}}"}
    for scale, projected, severity in ((1, 1200, "warning"), (0.01, 12, "critical")):
        draft = {**schema, "transforms": {"sequence": {"scale": scale}}}
        event = normalize(raw, SOURCES["generic-webhook"], [draft], received_at=RECEIVED)
        assert event.fields["$episode"] == "1200|1970-01-01T00:20:00+00:00"
        assert event.source_event_id == event.fields["$episode"] + "|tick.1200|warning"
        assert (event.service, event.schema_name, event.severity) == (str(projected), f"tick.{projected}", severity)
        assert event.fields["$time"] == datetime.fromtimestamp(projected, UTC).isoformat()


def test_masked_episode_identity_hashes_the_original_key_across_scale_changes():
    raw = {"kind": "tick", "token": 8731, "sequence": 1200}
    schema = {"id": "tick", "name": "tick", "detect": {"kind": "tick"},
              "episode_key": "{{token}}/{{sequence}}", "event_id": "{{$episode}}"}
    expected = "sha256-id:" + hashlib.sha256(b"8731/1200").hexdigest()
    for scale in (1, 0.01):
        draft = {**schema, "transforms": {path: {"scale": scale} for path in ("token", "sequence")}}
        event = normalize(raw, SOURCES["generic-webhook"], [draft], received_at=RECEIVED)
        assert event.source_event_id == expected
        assert event.fields["$episode"] == "<redacted>/1200" and "token.raw" not in event.fields


def test_unrelated_transform_preserves_the_existing_public_time_alias_identity():
    raw = {"kind": "tick", "token": 1200, "value": 80}
    schema = {"id": "tick", "name": "tick", "detect": {"kind": "tick"}, "event_id": "{{$time}}",
              "aliases": {"time": "token"}, "time_format": "epoch_s"}
    timestamp = "1970-01-01T00:20:00+00:00"
    for transforms in ({}, {"value": {"scale": 0.01}}):
        event = normalize(raw, SOURCES["generic-webhook"], [{**schema, "transforms": transforms}], received_at=RECEIVED)
        assert event.source_event_id == timestamp
        assert event.fields["$time"] == timestamp and event.fields["token"] == "<redacted>"


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
