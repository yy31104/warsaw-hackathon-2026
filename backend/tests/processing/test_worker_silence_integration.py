"""Exercise maintenance through the ordinary inbox transaction and scheduler."""

from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

pytest.importorskip("alerting.rules.silences", reason="requires the W1 rules integration")

from alerting.clock import SYSTEM, FixedClock
from alerting.rules.engine import Ruleset
from alerting.rules.scheduler import tick
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch


async def silence(db, at, duration):
    async with db.connection() as conn:
        await conn.execute(
            """insert into silences(team_id,matcher,created_by,created_at,expires_at)
               values('settlements',%s,'sam',%s,%s)""",
            (Jsonb({"maintenance": True}), at, at + duration),
        )


def settings():
    cfg = Settings.load()
    cfg.ruleset = Ruleset([{
        "id": "site", "owner": "team:settlements", "match": {"field": "site", "op": "exists"},
        "group": {"by": ["site"], "cooldown": "3m"},
        "route": [{"to": "user:sam", "channels": "preferences"}],
        "escalate_after": "1m", "ladder": ["ui", "email@2m"],
    }])
    return cfg


async def send(db, cfg, at, **data):
    async with db.connection() as conn:
        await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity,trace)
               values('sensor','http','synthetic','{}',%s,'sensor','warning',%s)""",
            (Jsonb({"site": "A", **data}), Jsonb({"demo_clock": at.isoformat()})),
        )
    assert await process_batch(db, cfg) == 1


async def test_silent_only_alert_waits_for_first_eligible_inbox_event(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg, at = settings(), SYSTEM.now()
    await silence(database, at - timedelta(seconds=1), timedelta(minutes=5))
    await send(database, cfg, at, maintenance=True)
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select outcome,reason->'silence'->>'created_by' from decisions where rule_id='site'"
        )).fetchone() == ("SILENCED", "sam")
        assert await (await conn.execute(
            "select occurrence_count,count_since_notify,next_notify_at from alerts"
        )).fetchone() == (1, 0, None)
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=6))) == 0
    await send(database, cfg, at + timedelta(minutes=7), maintenance=False)
    async with database.connection() as conn:
        assert await (await conn.execute("select kind,channel from notifications")).fetchall() == [
            ("ladder_step", "ui")
        ]
        assert await (await conn.execute(
            "select reason->>'notification_cause' from decisions where event_id=2 and rule_id='site'"
        )).fetchone() == ("first_eligible_after_silence",)
    # The time spent silent does not age the new escalation or first paid ladder step.
    await tick(database, cfg, FixedClock(at + timedelta(minutes=7, seconds=59)))
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select escalation_stage,ladder_stage from alerts"
        )).fetchone() == (0, 0)


async def test_active_window_holds_due_reminders_and_paid_stages_until_revoked(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg, at = settings(), SYSTEM.now()
    await send(database, cfg, at, maintenance=True)
    await silence(database, at + timedelta(seconds=1), timedelta(minutes=20))
    during = FixedClock(at + timedelta(minutes=4))
    await tick(database, cfg, during)
    assert await tick(database, cfg, during) == 0
    async with database.connection() as conn:
        assert await (await conn.execute("select kind,channel from notifications")).fetchall() == [
            ("ladder_step", "ui")
        ]
        assert await (await conn.execute("select escalation_stage,ladder_stage from alerts")).fetchone() == (0, 0)
        await conn.execute("update silences set revoked_at=%s", (during.now(),))
    assert await tick(database, cfg, during) == 1
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select escalation_stage,ladder_stage from alerts"
        )).fetchone() == (1, 1)


async def test_muted_due_batch_does_not_starve_the_next_eligible_incident(database):
    cfg, at = settings(), SYSTEM.now()
    rule = cfg.ruleset.rules[0][0]
    rule.pop("escalate_after")
    rule.pop("ladder")
    async with database.connection() as conn:
        await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,status)
               values('test','http','1','{}','{"maintenance":true}','processed'),
                     ('test','http','2','{}','{"maintenance":false}','processed')"""
        )
        await conn.execute(
            """insert into alerts(rule_id,rule_version,alert_key,team_id,severity,first_seen_at,
                  last_seen_at,last_notified_at,next_notify_at,occurrence_count,representative_event_id)
               select 'site',1,i::text,'settlements','warning',%s,%s,%s,%s,1,
                  case when i=205 then 2 else 1 end from generate_series(1,205) i""",
            (at, at, at, at),
        )
    await silence(database, at - timedelta(seconds=1), timedelta(hours=1))
    clock = FixedClock(at)
    assert await tick(database, cfg, clock) == 200
    assert await tick(database, cfg, clock) == 5
    assert await tick(database, cfg, clock) == 0
    async with database.connection() as conn:
        assert await (await conn.execute("select alert_id,kind from notifications")).fetchall() == [(205, "update")]
        assert await (await conn.execute(
            "select count(*) from audit_log where action='alert.timer_silenced'"
        )).fetchone() == (204,)
