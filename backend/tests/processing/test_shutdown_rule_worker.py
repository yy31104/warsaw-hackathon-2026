import asyncio
import os
import signal
import sys

from alerting.rules.engine import grouping
from alerting.rules.worker import Settings, process_batch

from .test_golden import CASES, case_event
from .test_worker import seed


async def test_stopped_worker_does_not_claim_waiting_events(database):
    await seed(database)
    stop = asyncio.Event()
    stop.set()
    assert await process_batch(database, Settings.load(), stop=stop) == 0
    async with database.connection() as conn:
        assert await (await conn.execute("select status from events")).fetchone() == ("pending",)


async def test_sigterm_drains_started_batch_and_leaves_next_batch_pending(database):
    await seed(database, count=2)
    ruleset = Settings.load().ruleset
    event = case_event(CASES[0])
    rule = next(r for r, predicate in ruleset.rules if predicate(event.fields)[0])
    key, _ = grouping(rule, ruleset.schemas[event.schema_id], event.fields)
    async with database.connection() as gate:
        # Hold the same key the first batch will need. pg_locks is a readiness handshake.
        await gate.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))",
                           (f"{rule['id']}:{key}",))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alerting.rules.worker", "--batch", "1",
            env={**os.environ, "DATABASE_URL": database.conninfo, "DB_LOCK_TIMEOUT_MS": "15000"},
        )
        try:
            async with asyncio.timeout(12):
                while not (await (await gate.execute(
                    """select exists(select from pg_locks where not granted and
                       locktype='advisory' and pid in (select pid from pg_stat_activity
                       where application_name=current_schema()) and (classid,objid) in
                       (select classid,objid from pg_locks where pid=pg_backend_pid()
                        and locktype='advisory' and granted))"""
                )).fetchone())[0]:
                    await asyncio.sleep(0.03)
            process.send_signal(signal.SIGTERM)
            await gate.commit()
            assert await asyncio.wait_for(process.wait(), timeout=10) == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    async with database.connection() as conn:
        assert await (await conn.execute("select status from events order by id")).fetchall() == [
            ("processed",), ("pending",),
        ]
        assert await (await conn.execute("select occurrence_count from alerts")).fetchone() == (1,)
