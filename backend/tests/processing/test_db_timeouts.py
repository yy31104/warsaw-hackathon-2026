import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from alerting import db
from alerting.rules.failures import transient
from .test_worker import Settings, process_batch, seed


async def test_hot_row_timeout_rolls_back_and_retries(database, monkeypatch):
    for key, val in (("LOCK", "40"), ("STATEMENT", "1000"), ("IDLE_IN_TRANSACTION", "3000")):
        monkeypatch.setenv("DB_" + key + "_TIMEOUT_MS", val)
    await seed(database, 1)
    cfg = Settings.load()
    await process_batch(database, cfg)
    await seed(database, 2)
    async with AsyncConnectionPool(
        database.conninfo, min_size=1, max_size=2, configure=db.configure_connection, open=False
    ) as workers:
        async with workers.connection() as conn:
            limits = await (
                await conn.execute(
                    "select current_setting('statement_timeout'), current_setting('lock_timeout'), current_setting('idle_in_transaction_session_timeout')"
                )
            ).fetchone()
            assert limits == ("1s", "40ms", "3s")
        async with database.connection() as blocker:
            await blocker.execute("select id from alerts for update")
            with pytest.raises(psycopg.errors.LockNotAvailable) as failure:
                await process_batch(workers, cfg)
            assert transient(failure.value)
            assert await (
                await blocker.execute(
                    "select count(*) from events where status='pending' and not trace ? 'attempts'"
                )
            ).fetchone() == (2,)
        assert await process_batch(workers, cfg) == 2
        async with database.connection() as conn:
            assert await (await conn.execute("select occurrence_count from alerts")).fetchall() == [
                (3,)
            ]
            assert await (await conn.execute("select count(*) from notifications")).fetchone() == (
                3,
            )
