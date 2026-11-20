from psycopg.types.json import Jsonb

from alerting.rules.worker import Settings, process_batch

from .test_worker import seed


async def test_explain_preserves_live_outcome_when_shadow_was_recorded_first(api, database):
    await seed(database)
    async with database.connection() as conn:
        await conn.execute(
            """insert into decisions (event_id,rule_id,rule_version,outcome,reason)
               values (1,'draft',1,'SHADOW_MATCH',%s)""",
            (Jsonb({"shadow": True, "notify_eligible": True}),),
        )
    assert await process_batch(database, Settings.load()) == 1
    response = await api.get("/v1/events/1/explain")
    assert response.status_code == 200
    report = response.json()
    assert report["outcome"] == "NEW_ALERT"
    assert report["decisions"][0]["outcome"] == "SHADOW_MATCH"
    assert report["notifications"]
