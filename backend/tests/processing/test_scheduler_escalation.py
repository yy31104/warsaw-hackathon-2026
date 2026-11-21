import asyncio
from datetime import timedelta

import pytest

from alerting.clock import SYSTEM, FixedClock
from alerting.rules.engine import Ruleset
from alerting.rules.scheduler import tick
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch

from .test_worker import seed


async def open_incident(db, rule_options, team_options=None):
    cfg = Settings.load()
    team = next(t for t in cfg.directory["teams"] if t["id"] == "settlements")
    team.update(team_options or {})
    rule = {
        "id": "scheduled-page",
        "owner": "team:settlements",
        "match": {"all": []},
        "route": [{"to": "oncall:settlements", "channels": "preferences"}],
        "group": {"cooldown": "2h"},
        **rule_options,
    }
    cfg.ruleset = Ruleset([rule])
    await seed(db)
    assert await process_batch(db, cfg) == 1
    async with db.connection() as conn:
        at, = await (await conn.execute("select first_seen_at from alerts")).fetchone()
    return cfg, at


@pytest.mark.parametrize("rule_interval", [None, "1m"])
async def test_unacked_escalates_to_secondary_then_approvers_exactly_once(database, rule_interval):
    # An explicit rule interval overrides the slower team setting.
    team = {"approvers": ["sam", "rita"], "escalate_after": "9m" if rule_interval else "1m"}
    cfg, at = await open_incident(database, {"escalate_after": rule_interval} if rule_interval else {}, team)
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=59))) == 0
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=1))) == 1
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=119))) == 0
    clock = FixedClock(at + timedelta(minutes=2))
    assert sum(await asyncio.gather(tick(database, cfg, clock), tick(database, cfg, clock))) == 1
    assert await tick(database, cfg, clock) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            """select recipient,channel,rendered->'meta'->'requires_open_alert' from notifications
               where kind='escalation' order by recipient"""
        )).fetchall() == [("lee", "sms", True), ("rita", "sms", True), ("sam", "sms", True)]
        assert await (await conn.execute(
            """select detail->>'step' from audit_log
               where action='alert.escalate' and target_kind='alert' and target_id='1' order by id"""
        )).fetchall() == [("secondary",), ("approvers",)]


async def test_ack_stops_escalation_before_the_next_step(database, api):
    cfg, at = await open_incident(database, {"escalate_after": "1m"}, {"approvers": ["sam"]})
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=1))) == 1
    assert (await api.post("/v1/alerts/1/ack", headers={"X-User": "sam"})).status_code == 200
    assert await tick(database, cfg, FixedClock(at + timedelta(hours=3))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select recipient from notifications where kind='escalation'"
        )).fetchall() == [("lee",)]
        assert await (await conn.execute(
            "select count(*) from audit_log where action='alert.escalate'"
        )).fetchone() == (1,)


async def test_live_poll_times_policy_steps_from_incident_creation(database):
    cfg, _ = await open_incident(database, {"escalate_after": "1m", "ladder": ["ui", "sms@1m"]})
    # A historical demo clock may put first_seen_at well before the actual creation.
    async with database.connection() as conn:
        await conn.execute("update alerts set first_seen_at=%s", (SYSTEM.now() - timedelta(days=30),))
    assert await tick(database, cfg) == 0
    async with database.connection() as conn:
        await conn.execute("update alerts set created_at=now()-interval '61 seconds'")
    assert await tick(database, cfg) == 1


async def test_unrelated_invalid_or_disabled_policy_never_blocks_an_escalation(database):
    cfg, at = await open_incident(database, {"escalate_after": "1m"})
    active = cfg.ruleset.rules[0][0]
    cfg.ruleset = Ruleset([
        active,
        {"id": "disabled", "enabled": False, "escalate_after": "invalid"},
        {"id": "broken", "owner": None, "escalate_after": "invalid", "ladder": ["ui", "sms@1m"], "route": [{}]},
    ])
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=1))) == 1


async def test_throttled_unacked_stages_keep_each_incidents_ack_boundary(database):
    cfg, at = await open_incident(database, {
        "escalate_after": "1m",
        "throttle": {"per_recipient": 0, "per": "1h"},
        "group": {"by": ["workflowId"], "cooldown": "2h"},
    })
    await seed(database, count=2)
    await process_batch(database, cfg)
    assert await tick(database, cfg, FixedClock(at + timedelta(seconds=61))) == 2
    async with database.connection() as conn:
        rows = await (await conn.execute(
            """select alert_id,rendered->'meta'->'requires_open_alert',throttle_bucket
               from notifications where recipient='lee' and kind='throttle_summary' order by alert_id"""
        )).fetchall()
    assert [(aid, gated) for aid, gated, _ in rows] == [(1, True), (2, True)]
    assert rows[0][2] != rows[1][2]
