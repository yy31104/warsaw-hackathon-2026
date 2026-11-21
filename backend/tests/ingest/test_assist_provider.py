"""Exercise the real SDK serializers and parsers without sending any network traffic."""

import asyncio
import inspect
import json
from unittest.mock import Mock

import anthropic
import httpx
import httpx2
import openai
import pytest
from pydantic import BaseModel

from alerting import llm
from alerting.api.app import app
from alerting.assist.rule_draft import draft_rule
from alerting.assist.runbook_structure import structure
from alerting.db import pool


class Extract(BaseModel):
    count: int


def configure(monkeypatch, provider):
    monkeypatch.setenv("ASSIST_ENABLED", "1")
    monkeypatch.setenv("ASSIST_PROVIDER", provider)
    monkeypatch.setenv("ASSIST_MODEL", "synthetic-model")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "mock-local-key")


def mock_wire(monkeypatch, provider, handler):
    sdk = openai if provider == "openai" else anthropic
    http_client = sdk.DefaultAsyncHttpxClient
    monkeypatch.setattr(sdk, "DefaultAsyncHttpxClient", lambda **kwargs: http_client(
        transport=httpx2.MockTransport(handler), **kwargs
    ))


def wire_response(provider, payload, *, refuse=False, incomplete=False):
    if provider == "openai":
        content = ({"type": "refusal", "refusal": "Declined"} if refuse else
                   {"type": "output_text", "text": payload, "annotations": []})
        return {
            "id": "resp_local", "object": "response", "created_at": 1, "model": "synthetic-snapshot",
            "status": "incomplete" if incomplete else "completed",
            "output": [{"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                        "content": [content]}],
            "usage": {"input_tokens": 21, "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
                      "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 26},
        }
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "synthetic-snapshot",
        "stop_reason": "refusal" if refuse else "max_tokens" if incomplete else "end_turn",
        "stop_sequence": None, "content": [{"type": "text", "text": "Declined" if refuse else payload}],
        "usage": {"input_tokens": 15, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2,
                  "output_tokens": 5},
    }


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("scenario,outcome", [
    ("valid", "success"), ("invalid", "invalid"), ("refusal", "refusal"), ("incomplete", "invalid"),
    ("timeout", "timeout"), ("http_timeout", "timeout"), ("unavailable", "error"),
])
async def test_sdk_round_trip(monkeypatch, caplog, provider, scenario, outcome):
    configure(monkeypatch, provider)
    requests, calls = [], []

    async def handle(request):
        requests.append(json.loads(request.content))
        if scenario == "timeout":
            await asyncio.sleep(30)
        if scenario == "http_timeout":
            raise httpx2.ReadTimeout("mock-local-key", request=request)
        if scenario == "unavailable":
            return httpx2.Response(503, json={"error": {"message": "mock-local-key private@example.invalid"}})
        wire = wire_response(provider, '{"count":"wrong"}' if scenario == "invalid" else '{"count":7}',
                             refuse=scenario == "refusal", incomplete=scenario == "incomplete")
        return httpx2.Response(200, json=wire)

    mock_wire(monkeypatch, provider, handle)
    with llm.observe_calls(calls.append, max_output_tokens=96):
        result = await llm.structured(Extract, system="classify", user="email private@example.invalid",
                                      timeout_s=0.2 if scenario == "timeout" else 8.0)
    assert result == (Extract(count=7) if outcome == "success" else None)
    request, = requests
    record, = calls
    assert record.provider == provider and record.model == request["model"] == "synthetic-model"
    assert record.outcome == outcome and record.latency_ms > 0
    assert "mock-local-key" not in caplog.text and "private@example.invalid" not in caplog.text
    if provider == "openai":
        assert request["text"]["format"]["type"] == "json_schema" and request["text"]["format"]["strict"]
        assert request["max_output_tokens"] == 96 and request["service_tier"] == "default" and request["store"] is False
        assert "<email>" in json.dumps(request["input"])
    else:
        assert request["output_config"]["format"]["type"] == "json_schema"
        assert request["max_tokens"] == 96 and "<email>" in json.dumps(request["messages"])
    if scenario in ("timeout", "http_timeout", "unavailable"):
        assert record.input_tokens is record.output_tokens is record.cached_input_tokens is None
    else:
        assert record.response_model == "synthetic-snapshot"
        assert (record.input_tokens, record.cached_input_tokens, record.output_tokens) == (21, 4, 5)
        assert record.cache_write_input_tokens == 2


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_http_status_and_schema_draft_follow_configuration_and_cache(monkeypatch, provider):
    configure(monkeypatch, provider)
    raw = {"component_locator": "synthetic-worker", "token": "private-secret",
           "observation": "check failed with token=private-secret", "clock": 1792833245000}
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        messages = body["input"] if provider == "openai" else body["messages"]
        fields = json.loads(messages[0]["content"])
        assert all(set(field) == {"path", "type", "example"} for field in fields)
        return httpx2.Response(200, json=wire_response(provider, '{"service":"component_locator"}'))

    mock_wire(monkeypatch, provider, handle)
    # POST with an inline sample performs no DB operations.
    monkeypatch.setitem(app.dependency_overrides, pool, lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        url = "/v1/schemas/suggest"
        status = await api.get("/v1/assist/status")
        assert status.json() == {"enabled": True, "provider": provider, "model": "synthetic-model"}
        for _ in range(2):
            response = await api.post(url, json={"sample": raw})
            assert response.status_code == 200
            draft = response.json()
            assert draft["source"] == "llm" and draft["model"] == "synthetic-model"
            assert draft["draft"]["aliases"]["service"] == "component_locator"
        assert len(requests) == 1 and "private-secret" not in json.dumps(requests)
        monkeypatch.setenv("ASSIST_MODEL", "second-model")
        new_draft = (await api.post(url, json={"sample": raw})).json()
        assert requests[-1]["model"] == new_draft["model"] == "second-model" and len(requests) == 2
        monkeypatch.delenv(f"{provider.upper()}_API_KEY")
        monkeypatch.setenv("ANTHROPIC_API_KEY" if provider == "openai" else "OPENAI_API_KEY", "other-key")
        assert (await api.get("/v1/assist/status")).json() == {
            "enabled": False, "provider": provider, "model": "second-model"
        }
        fallback = (await api.post(url, json={"sample": raw})).json()
        assert fallback["source"] == "heuristic" and "model" not in fallback and len(requests) == 2


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_text_drafts_report_model_including_cache_hits(monkeypatch, provider):
    configure(monkeypatch, provider)
    requested = []

    def handle(request):
        body = json.loads(request.content)
        system = body.get("instructions") or body["system"]
        if "alert rule" in system:
            requested.append("rule")
            payload = {"clauses": [[{"field": "capacity", "op": "lt", "value": 4}]],
                       "routes": [{"to": "team:lab", "channels": ["ui"]}]}
            if provider == "openai":
                channels = body["text"]["format"]["schema"]["$defs"]["RouteHint"]["properties"]["channels"]
                assert "default" not in channels
        else:
            requested.append("runbook")
            payload = {"steps": [{"index": 1, "action": "restart_service",
                                  "params": [{"name": "service", "value": "sample-api"}]}]}
        return httpx2.Response(200, json=wire_response(provider, json.dumps(payload)))

    mock_wire(monkeypatch, provider, handle)
    directory = {"users": [], "teams": [{"id": "lab", "name": "Lab Team"}], "fallback_team": "lab"}
    for _ in range(2):
        rule = await draft_rule("Handle capacity pressure in the lab.", directory)
        book = await structure("Bounce sample-api.")
        assert rule["source"] == book["source"] == "llm"
        assert rule["model"] == book["model"] == "synthetic-model"
        assert rule["draft"]["route"] == [{"to": "team:lab", "channels": ["ui"]}]
        assert book["draft"]["steps"][0]["approval"] == "required"
    assert requested == ["rule", "runbook"]


async def test_observers_are_task_local_and_restore_default_output_cap(monkeypatch):
    configure(monkeypatch, "openai")
    limits = []

    async def handle(request):
        await asyncio.sleep(0)
        limit = json.loads(request.content)["max_output_tokens"]
        limits.append(limit)
        return httpx2.Response(200, json=wire_response("openai", json.dumps({"count": limit})))

    mock_wire(monkeypatch, "openai", handle)

    async def measured(limit):
        records = []
        with llm.observe_calls(records.append, max_output_tokens=limit):
            result = await llm.structured(Extract, system="classify", user="label")
        assert result.count == limit
        return records

    left, right = await asyncio.gather(measured(95), measured(127))
    assert (await llm.structured(Extract, system="classify", user="label")).count == llm.MAX_OUTPUT_TOKENS
    assert len(left) == len(right) == 1
    assert sorted(limits) == [95, 127, llm.MAX_OUTPUT_TOKENS]


@pytest.mark.parametrize("key,value", [
    ("ASSIST_MODEL", ""), ("ASSIST_PROVIDER", ""), ("ASSIST_PROVIDER", "unknown"), ("ASSIST_ENABLED", "0"),
    ("OPENAI_API_KEY", "  "),
])
async def test_unconfigured_helper_skips_sdk(monkeypatch, key, value):
    configure(monkeypatch, "openai")
    monkeypatch.setenv(key, value)
    sdk = Mock(side_effect=AssertionError("unexpected provider initialization"))
    monkeypatch.setattr(openai, "AsyncOpenAI", sdk)
    assert llm.enabled() is False
    assert await llm.structured(Extract, system="test", user="test") is None
    sdk.assert_not_called()


def test_interactive_deadline():
    assert inspect.signature(llm.structured).parameters["timeout_s"].default == 8.0


async def test_free_count_matches_the_actual_openai_input(monkeypatch):
    configure(monkeypatch, "openai")
    wire, observed = {}, []

    def handle(request):
        path = request.url.path
        wire[path] = json.loads(request.content)
        if path == "/v1/responses/input_tokens":
            return httpx2.Response(200, json={"object": "response.input_tokens", "input_tokens": 197})
        assert path == "/v1/responses"
        return httpx2.Response(200, json=wire_response("openai", '{"count":1}'))

    mock_wire(monkeypatch, "openai", handle)
    args = {"system": "Count fixtures.", "user": "token=fake-sensitive-value private@example.invalid " + "ż" * 8000}
    with llm.observe_calls(observed.append, max_output_tokens=64):
        assert await llm.count_openai_input(Extract, **args) == 197
        assert not observed
        assert await llm.structured(Extract, **args) == Extract(count=1)
    count, generation = wire["/v1/responses/input_tokens"], wire["/v1/responses"]
    assert count == {key: generation[key] for key in ("model", "instructions", "input", "text")}
    assert count["text"]["format"]["strict"] is True and count["text"]["format"]["name"] == "Extract"
    assert len(count["input"][0]["content"].encode()) <= llm.MAX_INPUT_CHARS
    assert all(secret not in json.dumps(count) for secret in ("fake-sensitive-value", "private@example.invalid", "mock-local-key"))
    assert len(observed) == 1


@pytest.mark.parametrize("scenario", ["missing_key", "other_provider", "api_error", "invalid", "timeout"])
async def test_count_unavailable_returns_none(monkeypatch, caplog, scenario):
    configure(monkeypatch, "anthropic" if scenario == "other_provider" else "openai")
    requests = []

    async def handle(request):
        requests.append(request.url.path)
        if scenario == "timeout":
            await asyncio.sleep(30)
        if scenario == "api_error":
            return httpx2.Response(503, json={"error": {"message": "mock-local-key"}})
        return httpx2.Response(200, json={"object": "response.input_tokens", "input_tokens": -2})

    if scenario == "missing_key":
        monkeypatch.delenv("OPENAI_API_KEY")
    mock_wire(monkeypatch, "openai", handle)
    assert await llm.count_openai_input(Extract, system="test", user="test",
                                        timeout_s=0.2 if scenario == "timeout" else 8.0) is None
    assert requests == ([] if scenario in ("missing_key", "other_provider") else ["/v1/responses/input_tokens"])
    assert "mock-local-key" not in caplog.text
