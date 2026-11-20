from datetime import timedelta

import httpx
from alerting.clock import SYSTEM, FixedClock
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one

from .test_worker import Settings, process_batch, seed


async def test_channel_down_no_loss(database):
    await seed(database)
    await process_batch(database, Settings.load())
    keys = []

    def gateway(req):
        keys.append(req.headers["Idempotency-Key"])
        return httpx.Response(503 if len(keys) == 1 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        providers = {"sms": Telecom("sms", client)}
        assert await deliver_one(database, providers, ["sms"])
        assert await deliver_one(
            database, providers, ["sms"], FixedClock(SYSTEM.now() + timedelta(seconds=20))
        )
    async with database.connection() as conn:
        assert await (
            await conn.execute("select state,attempts from notifications where channel='sms'")
        ).fetchone() == ("delivered", 2)
    assert keys[0] == keys[1]


async def test_critical_fallback(database):
    await seed(database)
    await process_batch(database, Settings.load())
    async with database.connection() as conn:
        await conn.execute("update alerts set severity='critical'")
        await conn.execute(
            """insert into delivery_breakers values ('sms',5,now()-interval '35s',now()+interval '5s')"""
        )
    assert await deliver_one(database, {}, ["sms"])
    async with database.connection() as conn:
        assert await (
            await conn.execute("""select n.state,f.channel,f.fallback_of=n.id from notifications n join notifications f
            on f.fallback_of=n.id where n.channel='sms'""")
        ).fetchone() == ("fallback", "email", True)


async def test_cross_alert_throttle(database):
    settings = Settings.load()
    rule = settings.ruleset.rules[0][0]
    rule["group"]["by"] = ["workflowId"]
    rule["throttle"] = {"per_recipient": 1, "per": "10m"}
    await seed(database, 4)
    assert await process_batch(database, settings, batch=2) == 2
    assert await process_batch(database, settings, batch=2) == 2
    async with database.connection() as conn:
        rows = await (
            await conn.execute("""select kind, rendered->'meta'->>'suppressed_count' from notifications
            where recipient='sam' and channel='sms' order by created_at""")
        ).fetchall()
        assert rows == [("initial", None), ("throttle_summary", "3")]
