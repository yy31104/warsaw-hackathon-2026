from psycopg.types.json import Jsonb
from .test_golden import CASES, case_event
from .test_worker import Settings, process_batch


async def test_stale_recovery_keeps_newer_episode(database):
    event = case_event(next(c for c in CASES if c["id"] == "anna-daytime-sms"))
    cfg = Settings.load()

    async def send(status, episode):
        fields = dict(event.fields, status=status, startsAt=episode)
        fields["$episode"] = episode
        async with database.connection() as conn:
            await conn.execute(
                """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity)
                               values(%s,'http','test','{}',%s,%s,'critical')""",
                (event.source_id, Jsonb(fields), event.schema_id),
            )
        await process_batch(database, cfg)

    first, second = "2026-10-23T17:04:00Z", "2026-10-23T17:08:00Z"
    await send("firing", first)
    await send("firing", second)
    await send("resolved", first)
    async with database.connection() as conn:
        assert await (
            await conn.execute("select status,occurrence_count,latest_episode from alerts")
        ).fetchall() == [("open", 2, second)]
        assert await (
            await conn.execute(
                "select outcome,alert_id from decisions where event_id=3 and outcome='STALE_RESOLVE'"
            )
        ).fetchall() == [("STALE_RESOLVE", 1)]
    await send("resolved", second)
    async with database.connection() as conn:
        assert await (await conn.execute("select status,resolved_by from alerts")).fetchall() == [
            ("resolved", "system")
        ]
