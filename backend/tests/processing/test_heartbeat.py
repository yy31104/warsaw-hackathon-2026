import asyncio
from datetime import timedelta

from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, FixedClock
from alerting.rules.engine import Ruleset
from alerting.rules.scheduler import tick
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch


def heartbeat_settings():
    cfg = Settings.load()
    rule = {
        "id": "gauge-liveness",
        "owner": "team:rates",
        "match": {
            "all": [
                {"field": "type", "op": "glob", "value": "gau*"},
                {"field": "sample.count", "op": "gt", "value": 0},
            ]
        },
        "expect": {"every": "1m", "key": ["rack", "sensor"]},
        "route": [{"to": "user:rita", "channels": ["ui"]}],
        "present": {"title": "Missing gauge {{sensor}}"},
    }
    cfg.ruleset = Ruleset([rule], [{"id": "gauges"}])
    return cfg


async def beat(db, cfg, at, sensor, count=1, lane="normal"):
    fields = {"type": "gauge", "sample.count": count, "rack": "R", "sensor": sensor}
    async with db.connection() as conn:
        await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity,trace,lane)
               values('station','http','x','{}',%s,'gauges','info',%s,%s)""",
            (Jsonb(fields), Jsonb({"demo_clock": at.isoformat()}), lane),
        )
    assert await process_batch(db, cfg, lane=lane) == 1


async def test_deadman_tracks_each_composite_key_matches_dsl_and_resolves_on_next_beat(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = heartbeat_settings()
    at = SYSTEM.now()
    await beat(database, cfg, at, "A")
    await beat(database, cfg, at + timedelta(seconds=30), "B")
    await beat(database, cfg, at + timedelta(seconds=50), "A", count=0)
    async with database.connection() as conn:
        assert await (await conn.execute("select count(*) from alerts")).fetchone() == (0,)
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=59))) == 0
    clock = FixedClock(at + timedelta(minutes=1))
    assert sum(await asyncio.gather(tick(database, cfg, clock), tick(database, cfg, clock))) == 1
    assert await tick(database, cfg, clock) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select alert_key,status,title,representative_event_id from alerts"
        )).fetchall() == [("R|A", "open", "Missing gauge A", 1)]
        assert await (await conn.execute(
            "select event_id,reason->>'transition' from decisions where outcome='HEARTBEAT_MISSED'"
        )).fetchall() == [(1, "heartbeat_missed")]
        assert await (await conn.execute("select kind,recipient,state from notifications")).fetchall() == [
            ("heartbeat", "rita", "delivered")
        ]
    await beat(database, cfg, at + timedelta(seconds=61), "A")
    async with database.connection() as conn:
        assert await (await conn.execute("select status,resolved_by from alerts")).fetchall() == [
            ("resolved", "system")
        ]
        assert await (await conn.execute(
            "select event_id,alert_id from decisions where outcome='RESOLVED'"
        )).fetchall() == [(4, 1)]
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=89))) == 0
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=90))) == 1
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select alert_key from alerts where status='open'"
        )).fetchall() == [("R|B",)]


async def test_old_evidence_does_not_resolve_or_rearm_a_missed_heartbeat(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = heartbeat_settings()
    at = SYSTEM.now()
    await beat(database, cfg, at, "A")
    await tick(database, cfg, FixedClock(at + timedelta(minutes=1)))
    await beat(database, cfg, at - timedelta(seconds=2), "A", lane="critical")
    await beat(database, cfg, at + timedelta(seconds=30), "A", lane="critical")
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select status from alerts where alert_key='R|A'"
        )).fetchall() == [("open",)]
    await beat(database, cfg, at + timedelta(seconds=65), "A")
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=124))) == 0
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=125))) == 1
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select status from alerts order by id"
        )).fetchall() == [("resolved",), ("open",)]
