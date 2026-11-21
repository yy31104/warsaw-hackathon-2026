import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from alerting import llm
from alerting.assist.schema_suggest import FieldHints, suggest
from alerting.ingest.normalize import normalize
from alerting.schemas.mapping import SchemaDraft


def sample(raw):
    return normalize(raw, {"id": "sample-bus", "transport": "rabbitmq"}, [],
                     received_at=datetime(2026, 10, 23, tzinfo=UTC))


@pytest.mark.parametrize("raw,expected", [
    ({"app": {"name": "payments-api"}, "signal": "RED"}, {"service": "app.name", "severity": "signal"}),
    ({"service_name": "ledger", "logLevel": "WARN"}, {"service": "service_name", "severity": "logLevel"}),
    ({"component": "indexer", "deployment": {"stage": "test"}}, {"service": "component", "environment": "deployment.stage"}),
    ({"problem": {"code": "OVERHEAT"}, "description": "temperature high"}, {"error_code": "problem.code", "message": "description"}),
    ({"observedAt": "2026-10-23T09:00:00Z", "urgency": 3}, {"time": "observedAt", "severity": "urgency"}),
    ({"resource": {"service": "search"}, "env": "uat"}, {"service": "resource.service", "environment": "env"}),
    ({"application": "batch", "ts": 1792833245000}, {"service": "application", "time": "ts"}),
    ({"a17": 7, "z03": "x"}, {}),
])
async def test_offline_alias_eval(raw, expected, monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    call = AsyncMock()
    monkeypatch.setattr(llm, "structured", call)
    result = await suggest(sample(raw))
    draft = SchemaDraft.model_validate(result["draft"]).spec()
    assert result["source"] == "heuristic" and draft["aliases"] == expected
    event = normalize(raw, {"id": "test", "transport": "http"}, [draft], received_at=sample(raw).received_at)
    assert event.status == "pending"
    if "signal" in raw:
        assert event.severity == "unknown"
    if "ts" in raw:
        assert event.occurred_at.timestamp() == raw["ts"] / 1000
    call.assert_not_awaited()


async def test_model_only_fills_valid_missing_paths_and_is_cached(monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    call = AsyncMock(return_value=FieldHints(service="arbitrary", message="arbitrary", environment="absent", time="secret"))
    monkeypatch.setattr(llm, "structured", call)
    raw = {"service": "explicit", "arbitrary": "orange", "secret": "A-SYNTHETIC-SECRET", "counter": 1792833245000,
           "extra": {"email": "someone@example.invalid"}}
    one = await suggest(sample(raw))
    two = await suggest(sample(dict(raw, arbitrary="blue")))
    assert one["source"] == two["source"] == "llm"
    assert one["draft"]["aliases"] == two["draft"]["aliases"] == {"service": "service", "message": "arbitrary"}
    assert call.await_count == 1
    sent = call.await_args.kwargs["user"]
    assert len(sent.encode()) <= 8000 and "A-SYNTHETIC-SECRET" not in sent and "someone@example.invalid" not in sent
    assert all(set(row) == {"path", "type", "example"} for row in json.loads(sent))
    assert json.loads(llm.redact(sent)) and "1792833245000" not in sent


async def test_unavailable_model_returns_editable_draft(monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "structured", AsyncMock(return_value=None))
    result = await suggest(sample({"xyz_new": "z", "environment": "dev"}))
    assert result["source"] == "heuristic" and result["draft"]["aliases"] == {"environment": "environment"}


async def test_suggestion_api_is_read_only_and_works_from_an_issue(client, monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    raw = {"app": {"name": "invented"}, "priority": "fatal"}
    submitted = await client.post("/v1/events/generic-webhook", headers={"X-Api-Key": "demo-generic-key"}, json=raw)
    event_id, = submitted.json()["event_ids"]
    issue, = (await client.get("/v1/schema-issues")).json()["items"]
    for request in ({"sample": raw}, {"fingerprint": issue["fingerprint"]}):
        response = await client.post("/v1/schemas/suggest", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["source"] == "heuristic" and result["draft"]["aliases"]["service"] == "app.name"
        preview = await client.post("/v1/schemas/preview", params={"fingerprint": issue["fingerprint"]}, json=result["draft"])
        assert preview.json()["aliases"]["$severity"] == "critical"
    assert (await client.get(f"/v1/events/{event_id}")).json()["status"] == "needs_review"
    assert all(s["origin"] == "file" for s in (await client.get("/v1/schemas")).json()["items"])


async def test_time_hints_keep_units_without_revealing_digits(monkeypatch):
    call = AsyncMock(return_value=FieldHints())
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "structured", call)
    sample_time = "1729034400"
    result = await suggest(sample({"measure": {"event_epoch": sample_time}, "contactPhone": sample_time,
                                   "inventory": {"level": 12}, "importance_label": "WARN"}))
    assert result["draft"]["aliases"] == {"time": "measure.event_epoch", "severity": "importance_label"}
    assert result["draft"]["time_format"] == "epoch_s"
    payload = call.await_args.kwargs["user"]
    assert sample_time not in payload and "contactPhone" not in payload
    assert {r["path"]: r["example"] for r in json.loads(payload)}["measure.event_epoch"] == "<epoch_s>"


async def test_ambiguous_clock_and_severity_candidates_stay_unmapped(monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    result = await suggest(sample({"start_epoch": 1729034400, "end_epoch": 1729034600,
                                   "inventory": {"level": 12}, "status_a": "warn", "status_b": "info"}))
    assert result["draft"]["aliases"] == {}


async def test_event_clock_takes_precedence_over_related_object_creation(monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    raw = {"manifest": {"createdAt": "2024-12-01T09:00:00Z"}, "signal": {"onset_ms": 1735574410000}}
    draft = (await suggest(sample(raw)))["draft"]
    assert draft["aliases"]["time"] == "signal.onset_ms"
    assert draft["time_format"] == "epoch_ms"
