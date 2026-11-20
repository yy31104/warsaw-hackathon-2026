from datetime import timedelta

import pytest

from alerting.clock import FixedClock
from alerting.rules.scheduler import tick

from .test_scheduler_escalation import open_incident


LADDER = ["ui", "email@5m", "sms@15m", "phone@30m"]


async def test_delayed_ladder_stages_preserve_explicit_target_channels(database):
    cfg, at = await open_incident(database, {
        "ladder": LADDER,
        "route": [
            {"to": "user:rita", "channels": "preferences"},
            {"to": "user:sam", "channels": ["phone", "sms"]},
        ],
    })
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select recipient,channel from notifications order by recipient,channel"
        )).fetchall() == [("rita", "ui"), ("sam", "phone"), ("sam", "sms")]
    for minutes in (5, 15, 30):
        assert await tick(database, cfg, FixedClock(at + timedelta(minutes=minutes, microseconds=-1))) == 0
        assert await tick(database, cfg, FixedClock(at + timedelta(minutes=minutes))) == 1
        assert await tick(database, cfg, FixedClock(at + timedelta(minutes=minutes))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            """select recipient,channel from notifications where kind='ladder_step' and channel<>'ui'
               order by created_at"""
        )).fetchall() == [("rita", "email"), ("rita", "sms"), ("rita", "phone")]


async def test_ack_stops_ladder_and_explicit_only_rules_never_schedule_a_stage(database, api):
    cfg, at = await open_incident(database, {"ladder": LADDER})
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=5))) == 1
    assert (await api.post("/v1/alerts/1/ack", headers={"X-User": "sam"})).status_code == 200
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=30))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select count(*) from notifications where kind='ladder_step' and channel in ('sms','phone')"
        )).fetchone() == (0,)


async def test_explicit_only_never_arms_ladder(database):
    cfg, at = await open_incident(database, {
        "ladder": LADDER,
        "route": [{"to": "user:sam", "channels": ["sms"]}],
    })
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=31))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute("select kind,channel from notifications")).fetchall() == [
            ("initial", "sms")
        ]


@pytest.mark.parametrize("poll_delay", [timedelta(), timedelta(minutes=30)])
async def test_digest_ladder_waits_for_the_first_ui_digest_then_its_stage_offset(database, poll_delay):
    cfg, at = await open_incident(database, {
        "ladder": LADDER,
        "group": {"mode": "digest", "every": "10m"},
        "route": [
            {"to": "user:rita", "channels": "preferences"},
            {"to": "user:sam", "channels": ["sms"]},
        ],
    })
    async with database.connection() as conn:
        due, = await (await conn.execute("select next_notify_at from alerts")).fetchone()
    assert await tick(database, cfg, FixedClock(at + timedelta(minutes=5))) == 0
    first_digest = due + poll_delay
    assert await tick(database, cfg, FixedClock(first_digest)) == 1
    assert await tick(database, cfg, FixedClock(first_digest + timedelta(seconds=299))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select recipient,channel,kind from notifications order by recipient"
        )).fetchall() == [("rita", "ui", "ladder_step"), ("sam", "sms", "digest")]
        assert await (await conn.execute("select count_since_notify from alerts")).fetchone() == (0,)
    assert await tick(database, cfg, FixedClock(first_digest + timedelta(minutes=5))) == 1
    assert await tick(database, cfg, FixedClock(first_digest + timedelta(minutes=5))) == 0
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select recipient,channel from notifications where kind='ladder_step' order by channel"
        )).fetchall() == [("rita", "email"), ("rita", "ui")]
