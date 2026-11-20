from psycopg.types.json import Jsonb
from .database import database
from .test_golden import case_event, CASES
from alerting.rules.worker import process_batch, Settings


async def seed(pool, count=1, lane="normal"):
    event = case_event(CASES[0])
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany("""insert into events(source_id,transport,payload_hash,fields,raw,schema_id,severity,lane)
                values ('test','http','hash',%s,'{}','workflow','error',%s)""",
                [(Jsonb({**event.fields, "workflowId": f"WF-{i}"}), lane) for i in range(count)])


async def test_aggregation(database):
    await seed(database, 3)
    assert await process_batch(database, Settings.load(), "normal", 200) == 3
    async with database.connection() as conn:
        assert await (await conn.execute("select occurrence_count,team_id from alerts")).fetchone() == (3, "settlements")
        assert await (await conn.execute("select count(*) from notifications")).fetchone() == (3,)
        assert await (await conn.execute("select count(*) from events where trace ? 'processed_at'")).fetchone() == (3,)
