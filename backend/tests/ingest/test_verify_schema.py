import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from alerting.clock import FixedClock
from alerting.config import files
from alerting.ingest.normalize import normalize, reclassify
from alerting.rules.engine import Ruleset
from alerting.rules.safety import verify
from alerting.schemas.mapping import fingerprint


ROOT = Path(__file__).resolve().parents[3]
SOURCE = {"id": "generic-webhook", "transport": "http"}
DRAFT = {
    "id": "verification-holdout",
    "detect": {"origin": "verification-only"},
    "name": "reading.{{data.level}}",
    "aliases": {"severity": "data.level", "time": "data.at", "service": "host"},
    "severity_map": {"2": "warning"},
    "time_format": "epoch_s",
    "episode_key": "{{$service}}|{{data.at}}",
    "event_id": "{{$episode}}",
    "transforms": {
        "data.level": {"scale": "0.01", "unit": "level"},
        "data.at": {"scale": 0.001, "unit": "seconds"},
    },
}
READING = {"origin": "verification-only", "host": "sensor", "data": {"level": "200", "at": 5000}}


def test_review_verification_and_ingest_project_the_same_fields():
    at = datetime(2026, 10, 23, 10, tzinfo=UTC)
    sample = {**READING, "$episode": "producer override"}
    unknown = normalize(sample, SOURCE, [], received_at=at, message_id="transport-id")
    ingested = normalize(sample, SOURCE, [DRAFT], received_at=at)
    replayed = reclassify(unknown, [DRAFT])
    assert unknown.fields.get("$episode") is None
    assert replayed.fields == ingested.fields
    assert ingested.fields["data.level.raw"] == "200"
    assert ingested.fields["data.at.raw"] == 5000
    assert ingested.fields["$time"] == "1970-01-01T00:00:05+00:00"
    assert ingested.fields["$episode"] == ingested.source_event_id == "sensor|5"
    assert ingested.raw == replayed.raw == sample
    assert ingested.payload_hash == replayed.payload_hash
    assert ingested.schema_name == "reading.2" and ingested.severity == "warning"
    assert replayed.source_event_id == "transport-id"

    rule = {"id": "test", "match": {"$episode": "sensor|5"}, "route": [
        {"to": "user:anna", "channels": ["ui"]},
    ]}
    checked = verify(DRAFT, [{"event": sample, "expect": "notify", "source": SOURCE["id"]}],
                     Ruleset([rule]), files.directory(), [], FixedClock(at))
    assert checked["all_pass"]
    assert checked["samples"][0]["fields"] == ingested.fields
    assert checked["samples"][0]["deciding_fields"] == ["$episode"]


@pytest.mark.asyncio
async def test_verify_unit_change_contract_without_database_writes(client, inbox_pool):
    async def counts():
        async with inbox_pool.connection() as conn:
            row = await (await conn.execute("""
                SELECT (SELECT count(*) FROM config_objects), (SELECT count(*) FROM events),
                       (SELECT count(*) FROM schema_issues), (SELECT count(*) FROM alerts),
                       (SELECT count(*) FROM decisions), (SELECT count(*) FROM notifications),
                       (SELECT count(*) FROM audit_log), (SELECT count(*) FROM backend_file_rule_revisions)
            """)).fetchone()
            return row

    before = await counts()
    raw = json.loads((ROOT / "fixtures/events/trade_rejected_v2.json").read_text(encoding="utf-8"))
    request = {
        "schema": {"base": "business-envelope", "transforms": {
            "payload.notional": {"scale": 0.01, "unit": "EUR"},
        }},
        "samples": [
            {"event": {**raw, "payload": {**raw["payload"], "notional": 25_000_000}}, "expect": "quiet"},
            {"event": {**raw, "payload": {**raw["payload"], "notional": 2_500_000_000}},
             "expect": {"recipients": ["team:rates", "rita"], "channels": ["ui", "email"]}, "note": "EUR threshold"},
        ],
    }
    checked = await client.post("/v1/schemas/verify", json=request)
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert result["all_pass"]
    quiet, notify = result["samples"]
    assert quiet["actual"] == {"recipients": [], "channels": []}
    assert "payload.notional" in quiet["deciding_fields"]
    assert quiet["fields"]["payload.notional"] == 250_000
    assert quiet["fields"]["payload.notional.raw"] == 25_000_000
    assert not quiet["unit_warnings"]
    assert notify["note"] == "EUR threshold"
    assert notify["actual"] == {"recipients": ["rita", "team:rates"], "channels": ["email", "ui"]}

    request["schema"].pop("transforms")
    checked = await client.post("/v1/schemas/verify", json=request)
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert not result["all_pass"] and not result["samples"][0]["passed"]
    assert result["samples"][0]["unit_warnings"] == ["payload.notional"]
    assert await counts() == before


async def test_verified_fields_are_used_in_persisted_replay_and_new_ingest(client, inbox_pool):
    rule = {"id": "verify-live-rule", "match": {"$episode": "sensor|5", "data.level": 2},
            "route": [{"to": "user:ops", "channels": ["ui"]}]}
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            INSERT INTO config_objects (kind, id, version, spec, content_hash, origin)
            VALUES ('rule', %s, 1, %s, 'test-rule', 'ui')
        """, (rule["id"], Jsonb(rule)))

    sample = {"event": READING, "source": "generic-webhook",
              "expect": {"recipients": ["ops"], "channels": ["ui"]}}
    dry_run = await client.post("/v1/schemas/verify", json={"schema": DRAFT, "samples": [sample]})
    assert dry_run.status_code == 200 and dry_run.json()["all_pass"], dry_run.text
    result, = dry_run.json()["samples"]
    assert result["deciding_fields"] == ["$episode", "data.level"]

    async def send(key):
        response = await client.post("/v1/events/generic-webhook", json=READING,
                                     headers={"X-Api-Key": "demo-generic-key", "Idempotency-Key": key})
        assert response.status_code == 202, response.text
        event_id, = response.json()["event_ids"]
        return f"/v1/events/{event_id}"

    retained = await send("original")
    assert (await client.get(retained)).json()["status"] == "needs_review"
    published = await client.post("/v1/schemas", json=DRAFT, headers={"X-User": "ops"},
                                  params={"fingerprint": fingerprint(READING)})
    assert published.status_code == 200 and published.json()["reprocessed"] == 1, published.text
    for path in (retained, await send("subsequent")):
        event = (await client.get(path)).json()
        assert event["status"] == "pending" and event["schema_id"] == DRAFT["id"]
        assert event["fields"] == result["fields"]
        assert event["raw"] == READING


@pytest.mark.asyncio
@pytest.mark.parametrize(("draft", "sample"), [
    ({"base": "missing"}, {"event": {}, "expect": "quiet"}),
    ({**DRAFT, "transforms": {"data.level": {"scale": True}}}, {"event": READING, "expect": "quiet"}),
    (DRAFT, {"event": {}, "expect": {}}),
    (DRAFT, {"event": {}, "expect": {"channels": ["popup"]}}),
    (DRAFT, {"event": [], "expect": "quiet"}),
])
async def test_verify_rejects_malformed_drafts_and_samples(client, draft, sample):
    checked = await client.post("/v1/schemas/verify", json={"schema": draft, "samples": [sample]})
    assert checked.status_code == 422, checked.text
