import asyncio
import os
import sys

from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch

from .test_worker import seed


async def test_sigterm_drains_claimed_scheduler_batch_and_restart_finishes_backlog(database):
    settings = Settings.load()
    settings.ruleset.rules[0][0]["group"]["by"] = ["workflowId"]
    await seed(database, count=2)
    assert await process_batch(database, settings) == 2
    async with database.connection() as conn:
        await conn.execute("update alerts set next_notify_at=now() where id=1")
        # Block at the outbox insert. The child has already claimed its incident by this point.
        await conn.execute("""
            create function wait_for_scheduler_test() returns trigger language plpgsql as $$
            begin
              if new.kind='update' and new.alert_id=1 then
                perform pg_advisory_xact_lock(hashtextextended(current_schema()::text, 42));
              end if;
              return new;
            end $$;
            create trigger scheduler_barrier before insert on notifications
            for each row execute function wait_for_scheduler_test()""")

    async def spawn():
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alerting.rules.scheduler",
            env={
                **os.environ, "DATABASE_URL": database.conninfo,
                "DB_LOCK_TIMEOUT_MS": "15000", "DB_STATEMENT_TIMEOUT_MS": "20000",
            },
        )

    async def updates():
        async with database.connection() as conn:
            return await (await conn.execute(
                "select alert_id,count(*) from notifications where kind='update' group by alert_id order by alert_id"
            )).fetchall()

    children = []
    try:
        async with database.connection() as gate:
            await gate.execute("select pg_advisory_xact_lock(hashtextextended(current_schema()::text, 42))")
            child = await spawn()
            children.append(child)
            async with asyncio.timeout(12):
                while not (await (await gate.execute(
                    """select exists(select from pg_locks where not granted and locktype='advisory'
                       and (classid,objid) in (select classid,objid from pg_locks
                       where granted and locktype='advisory' and pid=pg_backend_pid()))"""
                )).fetchone())[0]:
                    await asyncio.sleep(0.03)
            await gate.execute("update alerts set next_notify_at=now() where id=2")
            child.terminate()
            await gate.commit()
            assert await asyncio.wait_for(child.wait(), 8) == 0
        assert await updates() == [(1, 3)]

        replacement = await spawn()
        children.append(replacement)
        async with asyncio.timeout(12):
            while await updates() != [(1, 3), (2, 3)]:
                await asyncio.sleep(0.05)
        replacement.terminate()
        assert await asyncio.wait_for(replacement.wait(), 8) == 0
        async with database.connection() as conn:
            assert await (await conn.execute(
                "select count(*),count(distinct dedup_key) from notifications"
            )).fetchone() == (12, 12)
            assert await (await conn.execute(
                "select count(*) from events where status='processed'"
            )).fetchone() == (2,)
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
