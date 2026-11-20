import asyncio

from .test_worker import Settings, process_batch, seed


async def test_storm_500(database):
    await seed(database, 500, lane="critical")
    settings = Settings.load()
    assert await process_batch(database, settings, "normal") == 0

    async def drain():
        total = 0
        while n := await process_batch(database, settings, "critical", 100):
            total += n
        return total

    assert sum(await asyncio.gather(drain(), drain())) == 500
    async with database.connection() as conn:
        assert await (
            await conn.execute("select status,occurrence_count from alerts")
        ).fetchall() == [("open", 500)]
        assert await (await conn.execute("select count(*) from alert_occurrences")).fetchone() == (
            50,
        )
        assert await (
            await conn.execute("select recipient,channel from notifications order by 1,2")
        ).fetchall() == [("sam", "phone"), ("sam", "sms"), ("team:settlements", "ui")]


async def test_inbound_sms(api, database):
    await seed(database)
    await process_batch(database, Settings.load())

    async def code():
        await api.post("/v1/alerts/1/actions", json={"step_index": 2}, headers={"X-User": "sam"})
        async with database.connection() as conn:
            return (
                await (
                    await conn.execute(
                        "select approval_code from action_runs order by id desc limit 1"
                    )
                ).fetchone()
            )[0]

    async def reply(sender, body):
        return await api.post("/v1/inbound/sms", json={"from": sender, "body": body})

    pin = await code()
    assert (await reply("+15550100002", "YES " + pin)).status_code == 403
    assert (await reply("+15550100003", "NO " + pin)).json()["state"] == "rejected"
    assert (await reply("+15550100003", "YES " + pin)).status_code in (404, 409)
    pin = await code()
    async with database.connection() as conn:
        await conn.execute(
            "update action_runs set approval_expires_at=now()-interval '1 minute' where approval_code=%s",
            (pin,),
        )
    assert (await reply("+15550100003", "YES " + pin)).status_code == 409
    pin = await code()
    assert (await reply("+15550100003", "YES " + pin)).json()["state"] == "succeeded"
