from datetime import timedelta

from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, FixedClock
from alerting.rules.engine import Ruleset
from alerting.rules.scheduler import tick
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch


def recovery_settings():
    cfg = Settings.load()
    rule = {
        "id": "pump-pressure",
        "owner": "team:rates",
        "match": {"signal": "high"},
        "route": [{"to": "user:rita", "channels": ["ui"]}],
        "group": {"by": ["pump"], "stable_for": "2m"},
    }
    schema = {"id": "sensor", "episode_key": "{{cycle}}", "resolve_when": {"signal": "ok"}}
    cfg.ruleset = Ruleset([rule], [schema])
    return cfg


async def signal(db, cfg, at, state, cycle):
    async with db.connection() as conn:
        await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity,trace)
               values('sensor','http','x','{}',%s,'sensor','warning',%s)""",
            (Jsonb({"pump": "A", "signal": state, "cycle": cycle}), Jsonb({"demo_clock": at.isoformat()})),
        )
    assert await process_batch(db, cfg) == 1


async def test_stable_for_uses_first_positive_evidence_and_exact_deadline(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = recovery_settings()
    at = SYSTEM.now()
    await signal(database, cfg, at, "high", "A")
    await signal(database, cfg, at + timedelta(seconds=1), "ok", "A")
    await signal(database, cfg, at + timedelta(seconds=60), "ok", "A")
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select status, resolve_pending_at, resolve_due_at, resolve_event_id from alerts"
        )).fetchone() == ("open", at + timedelta(seconds=1), at + timedelta(seconds=121), 2)
        assert await (await conn.execute(
            "select count(*) from decisions where outcome='RESOLVE_PENDING'"
        )).fetchone() == (2,)
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=120))) == 0
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=121))) == 1
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=122))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select status, resolved_by, resolve_pending_at, occurrence_count from alerts"
        )).fetchone() == ("resolved", "system", None, 1)
        assert await (await conn.execute(
            "select event_id,reason->>'transition' from decisions where outcome='RESOLVED'"
        )).fetchall() == [(2, "resolved")]


async def test_firing_cancels_pending_recovery_and_stale_episode_cannot_arm_it(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = recovery_settings()
    at = SYSTEM.now()
    await signal(database, cfg, at, "high", "A")
    await signal(database, cfg, at + timedelta(seconds=1), "ok", "A")
    await signal(database, cfg, at + timedelta(seconds=2), "high", "A")
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=121))) == 0
    await signal(database, cfg, at + timedelta(seconds=122), "ok", "A")
    await signal(database, cfg, at + timedelta(seconds=123), "high", "B")
    await signal(database, cfg, at + timedelta(seconds=124), "ok", "A")
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=245))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select status, latest_episode, resolve_pending_at, resolve_event_id from alerts"
        )).fetchone() == ("open", "B", None, None)
        assert await (await conn.execute(
            "select outcome from decisions where event_id=6 and rule_id='pump-pressure'"
        )).fetchone() == ("STALE_RESOLVE",)


async def test_scheduler_rechecks_episode_even_for_preexisting_pending_row(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = recovery_settings()
    at = SYSTEM.now()
    await signal(database, cfg, at, "high", "A")
    await signal(database, cfg, at, "ok", "A")
    async with database.connection() as conn:
        await conn.execute("update alerts set latest_episode='B'")
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=2))) == 1
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select status, resolve_pending_at, resolve_due_at from alerts"
        )).fetchone() == ("open", None, None)
