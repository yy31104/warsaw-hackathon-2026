from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alerting import llm
from alerting.clock import FixedClock
from alerting.schemas.hypotheses import generate_hypotheses


CLOCK = FixedClock(datetime(2026, 10, 23, 8, 30, tzinfo=UTC))
DRAFT = {"id": "sample-check", "name": "sample.reading", "detect": {"kind": "reading"},
         "aliases": {"severity": "rank", "time": "at"}}
SAMPLE = {"event": {"kind": "reading", "rank": "P1", "at": "2026-10-23 08:30:00",
                    "feeMinor": 400, "latency_ms": 20, "unscaled": 12345678,
                    "left": {"service": "one"}, "right": {"service": "two"}}}


def config(rules=()):
    return SimpleNamespace(revision=7, rules=list(rules), schemas=[], sources=[], runbooks=[], directory={
        "users": [{"id": "reader", "timezone": "Europe/Warsaw", "contacts": {"email": "reader@example.invalid"},
                   "preferences": [{"when": "08:00-09:00", "min_severity": "info", "channels": ["email"]}]}],
        "teams": [{"id": "ops", "members": ["reader"]}], "fallback_team": "ops", "default_preferences": [],
    })


def axes(snapshot, *, draft=DRAFT, samples=(SAMPLE,), declared=()):
    return {(axis["kind"], axis["path"]): axis
            for axis in generate_hypotheses(draft, samples, snapshot, declared, clock=CLOCK)}


def values(axis):
    return [candidate["value"] for candidate in axis["candidates"]]


def test_heuristics_use_suffix_magnitude_unmapped_rank_and_the_existing_alias_ranking():
    result = axes(config())
    assert values(result["scale", "feeMinor"]) == [1, 0.01]
    assert values(result["scale", "unscaled"]) == [1, 0.01]
    assert values(result["scale", "latency_ms"]) == [1, 0.01, 0.001]
    assert values(result["severity", "rank"]) == ["critical", "error", "warning", "info"]
    assert result["severity", "rank"]["label"] == "P1"
    assert values(result["alias", "service"]) == ["left.service", "right.service"]
    assert result["scale", "feeMinor"]["candidates"][1]["label"] == "×0.01 (e.g. cents to euros)"
    assert result["scale", "unscaled"]["candidates"][1]["label"] == "×0.01"
    assert all(candidate["source"] == "heuristic" for axis in result.values() for candidate in axis["candidates"])
    assert ("timezone", "at") not in result


def test_naive_time_requires_an_applicable_time_rule_or_a_routed_recipients_preference():
    route = {"to": "user:reader", "channels": "preferences"}
    timed = {"id": "clock", "match": {"$schema": "sample.reading"}, "route": [route]}
    assert values(axes(config([timed]))["timezone", "at"]) == ["UTC", "Europe/Warsaw"]
    unrelated = {**timed, "match": {"$schema": "other.name"}}
    assert ("timezone", "at") not in axes(config([unrelated]))
    explicit = {**timed, "route": [{**route, "channels": ["ui"]}]}
    assert ("timezone", "at") not in axes(config([explicit]))
    time_match = {**explicit, "match": {"all": [
        {"$schema": "sample.reading"}, {"field": "$time", "op": "exists"},
    ]}}
    assert ("timezone", "at") in axes(config([time_match]))
    aware = {"event": {**SAMPLE["event"], "at": "2026-10-23T08:30:00+02:00"}}
    assert ("timezone", "at") not in axes(config([timed]), samples=[aware])


def test_declared_mapping_and_explicit_candidates_preserve_their_provenance():
    declared = {"kind": "scale", "path": "feeMinor", "id": "fee-choice", "candidates": [
        {"value": 0.1, "source": "declared"}, {"value": 1, "source": "llm_suggestion"},
    ]}
    draft = {**DRAFT, "severity_map": {"p1": "warning"}, "transforms": {"latency_ms": {"scale": 0.001}}}
    result = axes(config(), draft=draft, declared=[declared])
    assert result["scale", "feeMinor"] == declared
    assert ("severity", "rank") not in result and ("scale", "latency_ms") not in result


def test_prefilled_alias_keeps_the_upstream_candidate_and_the_best_distinct_alternative():
    draft = {**DRAFT, "aliases": {**DRAFT["aliases"], "service": "right.service"}}
    item = {"kind": "alias", "path": "service", "candidates": [{"value": "right.service", "source": "llm_suggestion"}]}
    result = axes(config(), draft=draft, declared=[item])
    assert result["alias", "service"]["candidates"] == [
        {"value": "right.service", "source": "llm_suggestion"}, {"value": "left.service", "source": "heuristic"},
    ]
    original = axes(config(), draft=draft)["alias", "service"]
    assert values(original) == ["right.service", "left.service"]
    assert original["candidates"][0]["source"] == "declared"


def test_alternate_alias_can_introduce_its_own_unmapped_severity_and_naive_time():
    draft = {**DRAFT, "aliases": {"severity": "primary.severity", "time": "primary.timestamp"}}
    sample = {"event": {"kind": "reading",
                        "primary": {"severity": "error", "timestamp": "2026-10-23T08:30:00Z"},
                        "alternate": {"severity": "SEV2", "timestamp": "2026-10-23T08:30:00"}}}
    rule = {"id": "group-time", "match": {"$schema": "sample.reading"}, "group": {"by": ["$time"]},
            "route": [{"to": "user:reader", "channels": ["ui"]}]}
    result = axes(config([rule]), draft=draft, samples=[sample])
    assert result["severity", "alternate.severity"]["label"] == "SEV2"
    assert values(result["timezone", "alternate.timestamp"]) == ["UTC", "Europe/Warsaw"]
    assert ("severity", "primary.severity") not in result and ("timezone", "primary.timestamp") not in result


async def test_stability_endpoint_uses_pinned_read_only_config_and_does_not_modify_any_table(
    client, inbox_pool, monkeypatch
):
    from alerting.api import routes_schemas

    async def counts():
        async with inbox_pool.connection() as conn:
            rows = await conn.execute("""
                select (select count(*) from events), (select count(*) from alerts),
                       (select count(*) from decisions), (select count(*) from notifications),
                       (select count(*) from config_objects), (select count(*) from audit_log)
            """)
            return await rows.fetchone()

    before = await counts()
    snapshot_reader = routes_schemas.read_snapshot
    settings = []

    async def pinned(conn):
        row = await conn.execute("select current_setting('transaction_isolation'), current_setting('transaction_read_only')")
        settings.append(await row.fetchone())
        return await snapshot_reader(conn)

    monkeypatch.setattr(routes_schemas, "read_snapshot", pinned)
    provider = AsyncMock(side_effect=AssertionError("Stability evaluation must not call a provider"))
    monkeypatch.setattr(llm, "structured", provider)
    response = await client.post("/v1/schemas/stability", json={
        "draft": {"base": "business-envelope"},
        "samples": [{"source": "trade-bus", "event": {"meta": {"schema": "synthetic.trade"}, "payload": {"feeMinor": 400}}}],
        "hypotheses": [{"id": "fee", "kind": "scale", "path": "payload.feeMinor", "candidates": [
            {"value": 1, "source": "declared"}, {"value": 0.01, "source": "llm_suggestion"},
        ]}],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "INVARIANT" and result["semantics_confirmed"] is False
    assert result["evaluated_combinations"] == result["total_combinations"] == 2
    assert result["hypotheses"][0]["id"] == "fee"
    assert [candidate["source"] for candidate in result["hypotheses"][0]["candidates"]] == ["declared", "llm_suggestion"]
    assert result["snapshot"]["id"] and "revision" in result["snapshot"]
    assert settings == [("repeatable read", "on")]
    provider.assert_not_awaited()
    assert await counts() == before


@pytest.mark.parametrize("limit", ["time", "combinations"])
async def test_stability_api_reports_incomplete_when_it_cannot_finish(client, monkeypatch, limit):
    from alerting.api import routes_schemas

    if limit == "time":
        monkeypatch.setattr(routes_schemas, "TIME_LIMIT_SECONDS", 0)
    count = 65 if limit == "combinations" else 2
    response = await client.post("/v1/schemas/stability", json={
        "draft": DRAFT, "samples": [{"event": {"kind": "reading", "amount": 3}}],
        "hypotheses": [{"kind": "scale", "path": "amount", "candidates": [
            {"value": n, "source": "declared"} for n in range(1, count + 1)
        ]}],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total_combinations"] == count and result["evaluated_combinations"] <= 64
    assert result["status"] == "INCOMPLETE" and result["complete"] is False
    assert result["decision_invariant"] is None and result["semantics_confirmed"] is False


@pytest.mark.parametrize("body", [
    {"draft": {"base": "does-not-exist"}, "samples": [{"event": {}}]},
    {"draft": DRAFT, "samples": []},
    {"draft": DRAFT, "samples": [{"event": []}]},
    {"draft": DRAFT, "samples": [SAMPLE], "hypotheses": [{"kind": "scale", "path": "feeMinor",
        "candidates": [{"value": 1, "source": "unknown-source"}]}]},
])
async def test_stability_rejects_invalid_drafts_samples_and_provenance(client, body):
    assert (await client.post("/v1/schemas/stability", json=body)).status_code == 422


@pytest.mark.parametrize("kind,path,value", [
    ("timezone", "stamp", "Nowhere/City"),
    ("alias", "invented_alias", "sample.value"),
    ("severity", "rank", "urgent"),
    ("scale", "amount", "not a factor"),
])
def test_invalid_choice_values_are_rejected_before_replaying(kind, path, value):
    from alerting.api.routes_schemas import Hypothesis

    with pytest.raises(ValueError):
        Hypothesis.model_validate({"kind": kind, "path": path, "candidates": [{"value": value}]})
