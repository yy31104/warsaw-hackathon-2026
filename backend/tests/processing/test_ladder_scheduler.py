from datetime import timedelta

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
