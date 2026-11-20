import asyncio

from alerting.rules.engine import Ruleset
from alerting.rules.worker import Settings, process_batch


async def seed_sources(db, sources):
    async with db.connection() as conn:
        for source, count in sources.items():
            await conn.execute(
                """insert into events(source_id, transport, payload_hash, raw, fields)
                   select %s, 'http', 'test', '{}', '{}' from generate_series(1,%s)""",
                (source, count),
            )


async def processed(db):
    async with db.connection() as conn:
        return dict(await (await conn.execute(
            "select source_id,count(*) from events where status='processed' group by source_id"
        )).fetchall())


async def test_small_batches_rotate_while_all_sources_remain_backlogged(database):
    await seed_sources(database, {"a-noisy": 50, "b-quiet": 5, "c-quiet": 5})
    settings = Settings(Ruleset([]), {}, [])
    for _ in range(3):
        assert await process_batch(database, settings, batch=1) == 1
    assert await processed(database) == {"a-noisy": 1, "b-quiet": 1, "c-quiet": 1}


async def test_batch_shares_capacity_and_reuses_unused_quota(database):
    await seed_sources(database, {"a-noisy": 50, "b-medium": 20, "c-short": 1})
    settings = Settings(Ruleset([]), {}, [])
    assert await process_batch(database, settings, batch=12) == 12
    counts = await processed(database)
    assert counts["c-short"] == 1
    assert counts["a-noisy"] >= 4 and counts["b-medium"] >= 4
    await asyncio.gather(*(process_batch(database, settings, batch=20) for _ in range(3)))
    assert await processed(database) == {"a-noisy": 50, "b-medium": 20, "c-short": 1}
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select count(*),count(distinct event_id) from decisions"
        )).fetchone() == (71, 71)
