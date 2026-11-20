from alerting.rules.engine import Ruleset
from .test_worker import Settings, process_batch, seed


async def test_middle_event_isolated(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    await seed(database, 3)
    settings = Settings.load()
    async with database.connection() as conn:
        await conn.execute("""update events set trace='{"demo_fault":"raise"}' where id=2""")
    await process_batch(database, settings)
    async with database.connection() as conn:
        assert await (
            await conn.execute("select id,status from events order by id")
        ).fetchall() == [(1, "processed"), (2, "pending"), (3, "processed")]
    for _ in range(2):
        await process_batch(database, settings)
    async with database.connection() as conn:
        assert await (
            await conn.execute(
                "select status,trace->>'attempts',trace ? 'processing_error' from events where id=2"
            )
        ).fetchone() == ("quarantined", "3", True)
        assert await (
            await conn.execute("select outcome from decisions where event_id=2")
        ).fetchall() == [("QUARANTINED",)]
        assert await (await conn.execute("select occurrence_count from alerts")).fetchall() == [
            (2,)
        ]
        assert await (await conn.execute("select count(*) from notifications")).fetchone() == (3,)
    await seed(database)
    assert await process_batch(database, settings) == 1


async def test_bad_rule_is_scoped(database):
    await seed(database)
    cfg = Settings.load()
    good = cfg.ruleset.rules[0][0]
    bad = dict(good, id="invalid-rule", match={"field": "$service", "op": "unknown"})
    cfg.ruleset = Ruleset([bad, good], cfg.ruleset.schemas.values())
    await process_batch(database, cfg)
    async with database.connection() as conn:
        assert await (
            await conn.execute("select rule_id,outcome from decisions order by rule_id")
        ).fetchall() == sorted([(bad["id"], "RULE_FAULT"), (good["id"], "NEW_ALERT")])
        assert await (await conn.execute("select status from events")).fetchone() == ("processed",)
