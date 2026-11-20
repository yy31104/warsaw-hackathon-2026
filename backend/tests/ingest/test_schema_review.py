import asyncio
import json
import os
from contextlib import suppress
from uuid import uuid4

import aio_pika
import pytest
from pydantic import ValidationError

from alerting.config import files
from alerting.ingest.amqp import consume
from alerting.schemas.mapping import SchemaDraft, fingerprint


SOURCE = "/v1/events/generic-webhook"
KEY = {"X-Api-Key": "demo-generic-key"}
OPS = {"X-User": "ops"}
RAW = {"app": {"name": "payments-api"}, "signal": "RED"}
DRAFT = {"id": "vendor-signals", "name": "vendor.alarm",
         "detect": {"all": [{"field": "app.name", "op": "exists"}, {"field": "signal", "op": "exists"}]},
         "aliases": {"service": "app.name", "severity": "signal"}, "severity_map": {"RED": "critical"}}


def test_fingerprint_is_stable_bounded_and_unambiguous():
    assert fingerprint({"msg": 1, "at": 2, "kind": 3, "ctx": {}}) == "at,ctx,kind,msg"
    assert fingerprint(RAW) == fingerprint({"signal": 5, "app": {"other": True}})
    assert len({fingerprint(value) for value in ({}, {"": 1}, {"a,b": 1}, {"a": 1, "b": 1}, {"a%2Cb": 1})}) == 5
    assert len(fingerprint({"ł" * 2000: 1}).encode()) < 512


def test_draft_rejects_invalid_conditions_before_they_reach_ingest():
    for detect in ({"all": []}, {"field": "x", "op": "typo"}, {"field": "x", "op": "eq"},
                   {"not": {"any": [{"field": "a", "op": "exists"}]}, "all": []}):
        with pytest.raises(ValidationError):
            SchemaDraft.model_validate(dict(DRAFT, detect=detect))
    with pytest.raises(ValidationError):
        SchemaDraft.model_validate(dict(DRAFT, severity_map={"RED": "totally-new"}))
    with pytest.raises(ValidationError):
        SchemaDraft.model_validate(dict(DRAFT, name="bad\x00name"))


async def send(client, raw=RAW, key=None):
    response = await client.post(SOURCE, json=raw, headers={**KEY, **({"Idempotency-Key": key} if key else {})})
    assert response.status_code == 202, response.text
    return response.json()


async def test_review_preview_publish_and_original_event_handoff(client, inbox_pool):
    original = dict(RAW, password="private-example")
    first = await send(client, original, "first")
    event_id, = first["event_ids"]
    assert (await send(client, original, "first"))["duplicates"] == 1
    await send(client, original, "second")
    issues = (await client.get("/v1/schema-issues")).json()["items"]
    issue, = issues
    assert issue["event_count"] == 2 and issue["sample_event_id"] == event_id
    assert issue["sample_fields"]["app.name"] == "payments-api" and "private-example" not in json.dumps(issue)
    params = {"fingerprint": issue["fingerprint"]}
    before = (await client.get(f"/v1/events/{event_id}")).json()
    preview = await client.post("/v1/schemas/preview", params=params, json=DRAFT)
    assert preview.status_code == 200, preview.text
    assert preview.json()["matches"] and preview.json()["aliases"]["$severity"] == "critical"
    assert (await client.get(f"/v1/events/{event_id}")).json() == before
    assert (await client.post("/v1/schemas", params=params, json=DRAFT)).status_code == 401
    assert (await client.post("/v1/schemas", params=params, json=DRAFT, headers={"X-User": "anna"})).status_code == 403
    published = await client.post("/v1/schemas", params=params, json=DRAFT, headers=OPS)
    assert published.status_code == 200, published.text
    assert published.json()["reprocessed"] == 2 and published.json()["version"] == 1
    after = (await client.get(f"/v1/events/{event_id}")).json()
    for field in ("id", "source_event_id", "raw", "received_at", "payload_hash", "trace"):
        assert after[field] == before[field]
    assert (after["status"], after["lane"], after["schema_id"]) == ("pending", "critical", DRAFT["id"])
    # These are the actual field tests the existing Anna rule hands to Yi's engine.
    anna = next(rule for rule in files.rules() if rule["id"] == "anna-payments-critical")
    assert all(after["fields"][leaf["field"]] == leaf["value"] for leaf in anna["match"]["all"])
    assert (await client.get("/v1/schema-issues")).json()["items"] == []
    again = await client.post("/v1/schemas", params=params, json=DRAFT, headers=OPS)
    assert again.json()["reprocessed"] == 0 and again.json()["version"] == 1
    assert (await send(client, original, "first"))["duplicates"] == 1
    new_id, = (await send(client, dict(RAW, signal="red"), "third"))["event_ids"]
    assert (await client.get(f"/v1/events/{new_id}")).json()["severity"] == "critical"
    async with inbox_pool.connection() as conn:
        row = await (await conn.execute("""
            SELECT a.rule_id, a.team_id, a.status, n.channel, n.recipient, n.state
            FROM alerts a JOIN notifications n ON n.alert_id = a.id
        """)).fetchall()
        assert row == [("system:needs-review", "platform", "resolved", "ui", "team:platform", "delivered")]
        assert await (await conn.execute("SELECT count(*) FROM events")).fetchone() == (3,)
        assert await (await conn.execute("SELECT actor FROM audit_log WHERE action='schema.publish'")).fetchall() == [("ops",)]


async def test_mixed_shapes_keep_an_actionable_sample_and_preserve_distinct_ids(client):
    first = await send(client, RAW)
    other = await send(client, {"app": {"different": "library"}, "signal": "red"})
    issue, = (await client.get("/v1/schema-issues")).json()["items"]
    params = {"fingerprint": issue["fingerprint"]}
    draft = dict(DRAFT, event_id="{{app.name}}|{{signal}}")
    result = await client.post("/v1/schemas", params=params, headers=OPS, json=draft)
    assert result.json()["reprocessed"] == 1
    issue, = (await client.get("/v1/schema-issues")).json()["items"]
    assert issue["sample_event_id"] == other["event_ids"][0] and issue["event_count"] == 2
    repeat = await send(client, RAW)
    assert repeat["duplicates"] == 1 and repeat["event_ids"] == first["event_ids"]
    mismatch = await client.post("/v1/schemas", params=params, headers=OPS, json=dict(DRAFT, id="no-match"))
    assert mismatch.status_code == 422
    schemas = (await client.get("/v1/schemas")).json()["items"]
    assert not any(s["id"] == "no-match" for s in schemas)


async def test_concurrent_ingest_and_publish_leave_no_stranded_unknowns(client, inbox_pool):
    await asyncio.gather(*(send(client, RAW, f"first-{i}") for i in range(12)))
    issue, = (await client.get("/v1/schema-issues")).json()["items"]
    assert issue["event_count"] == 12
    async def approve():
        response = await client.post("/v1/schemas", params={"fingerprint": issue["fingerprint"]}, headers=OPS, json=DRAFT)
        assert response.status_code == 200, response.text
    await asyncio.gather(approve(), approve(), *(send(client, RAW, f"next-{i}") for i in range(12)))
    async with inbox_pool.connection() as conn:
        assert await (await conn.execute("SELECT status, count(*) FROM events GROUP BY status")).fetchall() == [("pending", 24)]
        assert await (await conn.execute("SELECT count(*) FROM notifications")).fetchone() == (1,)


async def test_ambiguous_new_identity_rolls_back_publication(client):
    await send(client, RAW)
    await send(client, dict(RAW, signal="orange"))
    issue, = (await client.get("/v1/schema-issues")).json()["items"]
    draft = dict(DRAFT, event_id="{{app.name}}")
    response = await client.post("/v1/schemas", json=draft, params={"fingerprint": issue["fingerprint"]}, headers=OPS)
    assert response.status_code == 409
    assert len((await client.get("/v1/schema-issues")).json()["items"]) == 1
    assert not any(s["id"] == "vendor-signals" for s in (await client.get("/v1/schemas")).json()["items"])


async def test_heuristic_identity_gives_way_to_configured_key_without_a_second_event(client, monkeypatch):
    registered = files.sources()
    monkeypatch.setattr(files, "sources", lambda: [dict(s, dedup="payload_hash") for s in registered])
    first = await send(client)
    draft = dict(DRAFT, event_id="{{app.name}}|{{signal}}")
    assert (await client.post("/v1/schemas", headers=OPS, json=draft)).json()["reprocessed"] == 1
    again = await send(client)
    assert again["duplicates"] == 1 and again["event_ids"] == first["event_ids"]


async def test_redacted_identity_cannot_be_reconstructed_by_guessing(client):
    await send(client, dict(RAW, token="synthetic-only"))
    result = await client.post("/v1/schemas", headers=OPS, json=dict(DRAFT, event_id="{{token}}"))
    assert result.status_code == 409 and "masked field" in result.json()["detail"]


async def test_rows_from_previous_release_are_listed_and_replayed(client, inbox_pool):
    from psycopg.types.json import Jsonb

    async with inbox_pool.connection() as conn:
        cursor = await conn.execute("""
            INSERT INTO events (source_id, transport, source_event_id, payload_hash, raw, fields, status)
            VALUES ('generic-webhook','http','legacy-key','original-digest',%s,%s,'needs_review') RETURNING id
        """, (Jsonb(RAW), Jsonb({"app.name": "payments-api", "signal": "RED"})))
        event_id, = await cursor.fetchone()
    for _ in range(2):
        issue, = (await client.get("/v1/schema-issues")).json()["items"]
        assert issue["event_count"] == 1 and issue["sample_event_id"] == event_id
    approved = await client.post("/v1/schemas", params={"fingerprint": issue["fingerprint"]}, headers=OPS, json=DRAFT)
    assert approved.json()["reprocessed"] == 1
    stored = (await client.get(f"/v1/events/{event_id}")).json()
    assert (stored["payload_hash"], stored["source_event_id"], stored["severity"]) == ("original-digest", "legacy-key", "critical")


async def test_new_version_preserves_file_ownership_and_refuses_stale_edits(client):
    assert (await client.post("/v1/schemas", json=dict(DRAFT, id="workflow"), headers=OPS)).status_code == 409
    created = await client.post("/v1/schemas", json=DRAFT, headers=OPS)
    assert created.json()["version"] == 1
    revised = dict(DRAFT, severity_map={"RED": "error"})
    result = await client.post("/v1/schemas?base_version=1", json=revised, headers=OPS)
    assert result.json()["version"] == 2
    assert (await client.post("/v1/schemas?base_version=1", json=DRAFT, headers=OPS)).status_code == 409
    event_id, = (await send(client, RAW))["event_ids"]
    event = (await client.get(f"/v1/events/{event_id}")).json()
    assert event["schema_version"] == 2 and event["severity"] == "error"


async def test_log_import_reads_the_approved_mapping(client, inbox_pool, tmp_path):
    from alerting.ingest.logfile import import_file

    assert (await client.post("/v1/schemas", json=DRAFT, headers=OPS)).status_code == 200
    source = {"id": "vendor-log", "transport": "logfile", "parser": {"pattern": "(?P<pairs>.*)", "kv_group": "pairs"}}
    path = tmp_path / "vendor.log"
    path.write_text("app.name=payments-api signal=RED\n", encoding="utf-8")
    assert await import_file(path, source, files.schemas(), inbox_pool) == {"ACCEPTED": 1}
    async with inbox_pool.connection() as conn:
        assert await (await conn.execute("SELECT service, severity, status FROM events")).fetchone() == (
            "payments-api", "critical", "pending")


async def test_running_rabbit_consumer_picks_up_http_approval(client, inbox_pool):
    url = os.getenv("INGEST_TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("set INGEST_TEST_RABBITMQ_URL for live cross-transport onboarding")
    source = {"id": "vendor-bus", "transport": "rabbitmq", "queue": "test-review-" + uuid4().hex}
    async with await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(source["queue"], durable=True)
        async with connection.channel() as consumer_channel:
            task = asyncio.create_task(consume(consumer_channel, [source], inbox_pool))
            try:
                async def send_and_read(key):
                    message = aio_pika.Message(json.dumps(RAW).encode(), message_id=key)
                    await channel.default_exchange.publish(message, routing_key=queue.name)
                    async with asyncio.timeout(10):
                        while True:
                            if task.done():
                                await task
                            async with inbox_pool.connection() as conn:
                                cursor = await conn.execute("SELECT id, status, severity FROM events WHERE source_event_id=%s", (key,))
                                if row := await cursor.fetchone():
                                    return row
                            await asyncio.sleep(0.05)

                first, status, _ = await send_and_read("before")
                assert status == "needs_review"
                issue, = (await client.get("/v1/schema-issues")).json()["items"]
                approved = await client.post("/v1/schemas", headers=OPS, params={"fingerprint": issue["fingerprint"]}, json=DRAFT)
                assert approved.json()["reprocessed"] == 1
                assert (await client.get(f"/v1/events/{first}")).json()["severity"] == "critical"
                _, status, severity = await send_and_read("after")
                assert (status, severity) == ("pending", "critical")
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await queue.delete(if_empty=False, if_unused=False)
