import asyncio
from collections import Counter

from alerting.contracts import SendResult
from alerting.delivery.worker import deliver_one


async def queue_sms(db, team, count, *, age, priority=3):
    async with db.connection() as conn:
        alert_id, = await (await conn.execute(
            """insert into alerts(rule_id,rule_version,alert_key,team_id) values ('test',1,%s,%s)
            returning id""", (f"{team}-{priority}", team),
        )).fetchone()
        await conn.execute(
            """insert into notifications(alert_id,kind,channel,recipient,contact,rendered,priority,next_attempt_at)
            select %s,'initial','sms','oncall',%s,'{"title":"test","body":"test","short":"test","meta":{}}',
                %s,now() - %s * interval '1 second' from generate_series(1,%s)""",
            (alert_id, team, priority, age, count),
        )


class Gateway:
    def __init__(self, release=None, count=0):
        self.receipts = []
        self.release = release
        self.count = count
        self.ready = asyncio.Event()

    async def send(self, msg, contact, idempotency_key):
        self.receipts.append((contact, idempotency_key))
        if len(self.receipts) >= self.count:
            self.ready.set()
        if self.release is not None:
            await self.release.wait()
        return SendResult(outcome="delivered")


async def test_noisy_team_round_robin_preserves_priority_and_drains_every_id(database):
    await queue_sms(database, "flood", 120, age=60)
    await queue_sms(database, "quiet-a", 3, age=5)
    await queue_sms(database, "quiet-b", 3, age=3)
    await queue_sms(database, "critical", 1, age=0, priority=1)
    async with database.connection() as conn:
        previous = await (await conn.execute(
            "select contact from notifications order by priority,next_attempt_at,id"
        )).fetchall()
    before = [row[0] for row in previous]

    gateway = Gateway()
    while await deliver_one(database, {"sms": gateway}, ["sms"]):
        pass
    teams = [team for team, _ in gateway.receipts]
    assert teams[0] == "critical"
    assert teams[1:10] == ["flood", "quiet-a", "quiet-b"] * 3
    assert Counter(teams) == {"critical": 1, "flood": 120, "quiet-a": 3, "quiet-b": 3}
    assert len({key for _, key in gateway.receipts}) == len(teams)
    print(f"N={len(teams)} quiet-b first send: FIFO={before.index('quiet-b') + 1}, fair={teams.index('quiet-b') + 1}")


async def test_concurrent_senders_claim_different_teams_before_any_provider_finishes(database):
    for team, age in [("flood", 60), ("quiet-a", 5), ("quiet-b", 3)]:
        await queue_sms(database, team, 8, age=age)
    release = asyncio.Event()
    gateway = Gateway(release, 3)
    tasks = [asyncio.create_task(deliver_one(database, {"sms": gateway}, ["sms"])) for _ in range(3)]
    try:
        await asyncio.wait_for(gateway.ready.wait(), 5)
        assert {team for team, _ in gateway.receipts} == {"flood", "quiet-a", "quiet-b"}
        async with database.connection() as conn:
            assert (3,) == await (await conn.execute(
                "select count(*) from notifications where state='sending' and locked_by is not null"
            )).fetchone()
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_outage_backlog_shares_fallback_and_keeps_healthy_channel_moving(database):
    await queue_sms(database, "flood", 12, age=60)
    await queue_sms(database, "quiet-fallback", 2, age=5)
    await queue_sms(database, "healthy", 2, age=3)
    async with database.connection() as conn:
        await conn.execute("update notifications set channel='phone' where contact='healthy'")
        await conn.execute("update alerts set severity='critical'")
        await conn.execute(
            "insert into delivery_breakers values ('sms',5,now()-interval '35 seconds',now()+interval '60 seconds')"
        )

    gateway = Gateway()
    for _ in range(2):
        await deliver_one(database, {"phone": gateway}, ["sms", "phone"])
    assert [team for team, _ in gateway.receipts] == ["healthy", "healthy"]
    async with database.connection() as conn:
        converted = await (await conn.execute(
            "select contact from notifications where state='fallback' order by contact"
        )).fetchall()
    assert converted == [("flood",), ("quiet-fallback",)]
