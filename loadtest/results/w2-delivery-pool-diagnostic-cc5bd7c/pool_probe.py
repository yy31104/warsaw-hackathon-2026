"""In-memory connection-pool probe for the delivery worker in a disposable trial."""
import atexit
import json
from pathlib import Path
from time import perf_counter_ns, time_ns

from psycopg_pool import AsyncConnectionPool, __version__
from psycopg_pool.pool_async import WaitingClient

checkouts = []
queued = []
getconn = AsyncConnectionPool.getconn
wait = WaitingClient.wait


async def observe_checkout(self, *args, **kwargs):
    started_at = time_ns()
    started = perf_counter_ns()
    ok = False
    try:
        conn = await getconn(self, *args, **kwargs)
        ok = True
        return conn
    finally:
        checkouts.append((started_at, perf_counter_ns() - started, ok))


async def observe_queue(self, *args, **kwargs):
    started_at = time_ns()
    started = perf_counter_ns()
    ok = False
    try:
        conn = await wait(self, *args, **kwargs)
        ok = True
        return conn
    finally:
        queued.append((started_at, perf_counter_ns() - started, ok))


AsyncConnectionPool.getconn = observe_checkout
WaitingClient.wait = observe_queue


@atexit.register
def flush():
    result = {"psycopg_pool": __version__,
              "schema": ["started_at_unix_ns", "elapsed_monotonic_ns", "success"],
              "checkout": checkouts, "queue_wait": queued}
    Path("/w0-pool-output/delivery-pool.json").write_text(json.dumps(result) + "\n")
