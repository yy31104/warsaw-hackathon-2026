from datetime import timedelta

from alerting.clock import SYSTEM, FixedClock
from alerting.rules.scheduler import tick
from alerting.rules.scoring import score

from .test_worker import Settings, process_batch, seed


def test_score():
    priority, total, parts = score("error", "prod", 1000, {"impact": 99, "priority": "P0"})
    assert priority == "P0" and total == 100
    assert parts == {"severity": 35, "environment": 25, "impact": 25, "frequency": 15}


async def test_digest_counts_and_explain(database, api):
    async with database.connection() as conn:
        await conn.execute("""insert into events(source_id,transport,payload_hash,fields,raw,schema_id,severity)
            select 'pricing-logs','logfile','x','{"instance":"pricing-engine-1","instrument":"EURUSD","$severity":"warning"}',
            '{}','pricing-log','warning' from generate_series(1,3)""")
    settings = Settings.load()
    await process_batch(database, settings)
    clock = FixedClock(SYSTEM.now() + timedelta(minutes=6))
    assert await tick(database, settings, clock) == 1
    assert await tick(database, settings, clock) == 0
    async with database.connection() as conn:
        assert (
            "3 occurrences"
            in (
                await (await conn.execute("select rendered->>'body' from notifications")).fetchone()
            )[0]
        )
    data = (await api.get("/v1/events/1/explain")).json()
    assert data["outcome"] == "DIGEST_QUEUED" and len(data["decisions"]) == 4


async def test_cooldown(database):
    await seed(database, 2)
    cfg = Settings.load()
    await process_batch(database, cfg)
    assert await tick(database, cfg, FixedClock(SYSTEM.now() + timedelta(minutes=11))) == 1
    async with database.connection() as conn:
        assert await (
            await conn.execute("select count(*) from notifications where kind='update'")
        ).fetchone() == (3,)


async def test_idle_reminder_stops_after_ack(database, api):
    await seed(database)
    settings = Settings.load()
    await process_batch(database, settings)
    clock = FixedClock(SYSTEM.now() + timedelta(minutes=11))
    assert await tick(database, settings, clock) == 1
    assert await tick(database, settings, clock) == 0
    assert (await api.post("/v1/alerts/1/ack", headers={"X-User": "sam"})).status_code == 200
    assert await tick(database, settings, FixedClock(clock.now() + timedelta(minutes=11))) == 0
