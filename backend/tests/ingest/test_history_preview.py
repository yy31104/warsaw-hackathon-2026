from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.types.json import Jsonb

from alerting.api.routes_rule_assist import router
from alerting.config import files
from alerting.contracts import CanonicalEvent
from alerting.db import pool
from alerting.simulate.history import preview_history


ORIGIN = datetime(2026, 11, 20, 7, tzinfo=UTC)
CURRENT = {
    "id": "inventory-replay",
    "owner": "team:rates",
    "match": {"field": "inventory.count", "op": "lt", "value": 10},
    "route": [{"to": "user:sam", "channels": ["sms"]}],
    "group": {"by": ["inventory.bin"], "cooldown": "10m"},
}
DRAFT = {
    **CURRENT,
    "match": {"field": "inventory.count", "op": "lt", "value": 4},
    "route": [{"to": "user:anna", "channels": ["ui"]}],
}


def event(id, *, count=3, bin="a", minutes=0, severity="warning", status="processed", fields=None):
    return CanonicalEvent(
        id=id, source_id="inventory-test", transport="http", payload_hash=f"test-{id}",
        received_at=ORIGIN + timedelta(minutes=minutes), severity=severity, raw={}, status=status,
        fields={"$severity": severity, "inventory.count": count, "inventory.bin": bin, **(fields or {})},
    )


def test_replay_groups_and_projects_cooldowns_in_both_rule_versions():
    events = [event(1), event(2, minutes=1), event(3, count=7, bin="b", minutes=2), event(4, minutes=11)]
    report = preview_history(events, CURRENT, DRAFT, [], files.directory(), [])
    assert report["events_replayed"] == 4
    assert report["current"] == {"alerts": 2, "matched_events": 4, "notifications": {"sam/sms": 3}}
    assert report["draft"] == {"alerts": 1, "matched_events": 3, "notifications": {"anna/ui": 2}}
    assert report["lost_all_notifications"] == [3]
    assert report["lost_recipients"] == ["sam"] and report["new_recipients"] == ["anna"]
    assert [row["event_id"] for row in report["events_with_recipient_loss"]] == [1, 3, 4]


def test_due_digest_credits_every_queued_event_and_stops_at_history_end():
    current = {**CURRENT, "group": {**CURRENT["group"], "mode": "digest", "every": "5m"}}
    events = [event(1), event(2, minutes=1), event(3, bin="b", minutes=6)]
    report = preview_history(events, current, {**current, "enabled": False}, [], files.directory(), [])
    assert report["current"] == {"alerts": 2, "matched_events": 3, "notifications": {"sam/sms": 1}}
    assert report["draft"]["notifications"] == {}
    assert report["lost_all_notifications"] == [1, 2]


def test_historical_clock_selects_preferences_and_throttle_bucket_counts_logical_intents():
    rules = {
        **CURRENT,
        "route": [{"to": "user:anna", "channels": "preferences"}],
        "throttle": {"per_recipient": 1, "per": "10m"},
    }
    events = [event(i, bin=str(i), severity="critical", minutes=i) for i in range(1, 5)]
    # 07:01 UTC is 08:01 Warsaw in November. The later occurrence uses the night preference.
    events.append(event(5, bin="e", severity="critical", minutes=800))
    report = preview_history(events, None, rules, [], files.directory(), [])
    assert report["draft"]["notifications"] == {"anna/email": 1, "anna/sms": 2}
    assert report["draft"]["alerts"] == 5


async def _snapshot(conn):
    tables = ("alerts", "events", "notifications", "delivery_attempts", "decisions", "audit_log",
              "rule_proposals", "config_objects", "backend_file_rule_revisions", "delivery_windows", "action_runs")
    snapshot = {}
    for table in tables:
        query = sql.SQL("select coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)), '[]') from {} t")
        snapshot[table] = (await (await conn.execute(query.format(sql.Identifier(table)))).fetchone())[0]
    return snapshot


async def test_history_and_safety_api_use_committed_inputs_without_writes(inbox_pool, monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            insert into config_objects(kind,id,version,spec,content_hash,origin,created_by)
            values ('rule', %s, 1, %s, 'test', 'ui', 'sam')
        """, (CURRENT["id"], Jsonb(CURRENT)))
        event_ids = []
        for sample in [event(1), event(2, minutes=1, bin="b", count=7), event(3, status="needs_review")]:
            row = await (await conn.execute("""
                insert into events(source_id,transport,payload_hash,severity,received_at,raw,fields,status)
                values (%s,%s,%s,%s,%s,%s,%s,%s) returning id
            """, (sample.source_id, sample.transport, sample.payload_hash, sample.severity.value, sample.received_at,
                  Jsonb(sample.raw), Jsonb(sample.fields), sample.status))).fetchone()
            event_ids.append(row[0])
        before = await _snapshot(conn)

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[pool] = lambda: inbox_pool
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        replay = await client.post(f"/v1/rules/{CURRENT['id']}/preview", json={"draft": DRAFT, "last_n": 2})
        assert replay.status_code == 200, replay.text
        data = replay.json()
        assert data["sample_event_ids"] == event_ids[:2]
        assert data["lost_all_notifications"] == [event_ids[1]]
        assert data["current"]["notifications"] == {"sam/sms": 2}
        assert data["draft"]["notifications"] == {"anna/ui": 1}
        sample = await client.post("/v1/rules/test", json={"rule": DRAFT, "event": event(1).fields})
        assert sample.status_code == 200 and sample.json()["matched"], sample.text
        assert sample.json()["recipients"] == ["anna"]
        draft = await client.post("/v1/rules/draft", json={"text": "Notify Sam when inventory.count < 4"})
        assert draft.status_code == 200 and draft.json()["source"] == "heuristic", draft.text
        invalid = await client.post("/v1/rules/check", json={"rule": {
            "match": {"$severity": "critical"}, "route": [{"to": "oncall:missing-team", "channels": ["sms"]}],
        }})
        assert invalid.status_code == 200 and len(invalid.json()["scenarios_checked"]) >= 4, invalid.text
        assert any("reaches nobody" in v["problem"] for v in invalid.json()["violations"])
        ui_only = await client.post("/v1/rules/check", json={"rule_id": "pricing-warn-logs"})
        assert ui_only.status_code == 200 and not ui_only.json()["violations"], ui_only.text
        missing = await client.post("/v1/rules/check", json={"rule_id": "missing"})
        assert missing.status_code == 404

    async with inbox_pool.connection() as conn:
        assert await _snapshot(conn) == before
