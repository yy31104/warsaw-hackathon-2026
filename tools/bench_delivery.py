"""Time the delivery outbox with local PostgreSQL and an in-process provider.

Set DATABASE_URL to a disposable local database. Each cell gets a fresh schema
and one connection pool per in-process worker. Timing covers claim through
committed completion and excludes setup and verification. Run both modes from
the new checkout.

    git worktree add --detach /tmp/delivery-before d7a6884
    PYTHONPATH=/tmp/delivery-before/backend backend/.venv/bin/python tools/bench_delivery.py --mode baseline --workers 1 4
    PYTHONPATH=backend backend/.venv/bin/python tools/bench_delivery.py --mode batch --workers 1 4

Add --workload mixed to each command for 40 critical and 1,960 normal notices.
Every fiftieth notice is critical and all are due together. The nearest-rank
p99 runs from the common worker start to the local receipt after its 5 ms delay.
Backlog samples count outstanding PostgreSQL rows, including active leases.
"""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import math
import os
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from alerting.contracts import SendResult
from alerting.db import configure_connection
from alerting.delivery import worker
from alerting.delivery.shutdown import wait_or_stop
from alerting.rules.storage import close_settings, get_settings, prepare_settings


NOTICES = 2000
DELAY = 0.005
BATCH_SIZE = 25
CHANNEL_LIMIT = 16
CRITICAL_EVERY = 50


class LocalSMS:
    def __init__(self):
        self.receipts = []
        self.in_flight = 0
        self.peak = 0

    async def send(self, message, contact, idempotency_key):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(DELAY)
            self.receipts.append((idempotency_key, perf_counter()))
            return SendResult(outcome="delivered", provider_ref=idempotency_key)
        finally:
            self.in_flight -= 1


def local_dsn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError("Set DATABASE_URL to the disposable local PostgreSQL database.")
    params = conninfo_to_dict(dsn)
    if params.get("service") or os.environ.get("PGSERVICE"):
        raise ValueError("Provide the local database address directly.")
    for key in ("host", "hostaddr"):
        addresses = params.get(key) or os.environ.get("PG" + key.upper(), "")
        for host in addresses.split(","):
            if host in ("", "localhost") or host.startswith("/"):
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                raise ValueError("Use a loopback IP, localhost, or a local socket.") from None
            if not address.is_loopback:
                raise ValueError("This benchmark requires a local PostgreSQL address.")
    return dsn


@asynccontextmanager
async def benchmark_schema(dsn, workload):
    name = "bench_delivery_" + uuid4().hex
    identifier = sql.Identifier(name)
    migrations = Path(worker.__file__).resolve().parents[3] / "db" / "migrations"
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("create schema {}").format(identifier))
        try:
            await admin.execute(sql.SQL("set search_path to {}").format(identifier))
            for migration in sorted(migrations.glob("*.sql")):
                await admin.execute(migration.read_text(encoding="utf-8"))
            await admin.execute(
                """with seed as (
                     select i, %s and mod(i,%s)=0 as critical from generate_series(1,%s) i
                   ), incidents as (
                     insert into alerts(rule_id,rule_version,alert_key,team_id,severity,priority,lane)
                     select 'benchmark',1,i::text,'synthetic-team',
                            case when critical then 'critical' else 'warning' end,
                            case when critical then 'P0' else 'P2' end,
                            case when critical then 'critical' else 'normal' end
                     from seed returning id,alert_key,lane
                   )
                   insert into notifications(id,alert_id,kind,recipient,channel,contact,rendered,dedup_key,priority)
                   select md5('bench-'||alert_key)::uuid,id,'initial','synthetic-recipient','sms',
                          'synthetic-contact',%s,'bench-'||alert_key,case when lane='critical' then 1 else 3 end
                   from incidents""",
                (workload == "mixed", CRITICAL_EVERY, NOTICES,
                 Jsonb({"title": "Synthetic", "body": "Synthetic", "short": "Synthetic"})),
            )
            await admin.execute("analyze alerts")
            await admin.execute("analyze notifications")
            scoped = make_conninfo(dsn, options=f"-c search_path={name}", application_name=name)
            yield scoped, admin
        finally:
            await admin.execute(sql.SQL("drop schema {} cascade").format(identifier))


async def verify(db, receipts):
    async with db.connection() as conn:
        notices = await conn.execute(
            """select id::text, state='delivered' and attempts=1 and delivered_at is not null
                      and locked_by is null and locked_until is null from notifications"""
        )
        attempts = await conn.execute(
            """select notification_id::text, attempt=1 and outcome='delivered' and finished_at is not null
                      and provider_ref=notification_id::text from delivery_attempts"""
        )
        notices = await notices.fetchall()
        attempts = await attempts.fetchall()
    delivered = {key for key, settled in notices if settled}
    finished = {key for key, settled in attempts if settled}
    received = {key for key, _ in receipts}
    if (
        len(notices) != NOTICES
        or len(attempts) != NOTICES
        or len(receipts) != NOTICES
        or len(delivered) != NOTICES
        or delivered != finished
        or delivered != received
    ):
        raise RuntimeError(
            f"Delivery reconciliation failed: seeded={NOTICES}, completed={len(delivered)}, "
            f"calls={len(receipts)}, unique_keys={len(received)}, finished_attempts={len(finished)}"
        )
    return len(delivered), len(attempts)


async def pending(conn):
    result = await conn.execute(
        "select count(*) from notifications where state in ('pending','sending','retrying','unknown')"
    )
    return (await result.fetchone())[0]


async def sample_backlog(conn, start, finished, samples):
    for offset in (0.25, 1, 2, 5, 10, 20, 30, 60, 120, 240):
        await wait_or_stop(finished, max(0, start + offset - perf_counter()))
        if finished.is_set():
            return
        remaining = await pending(conn)
        samples.append([round(perf_counter() - start, 3), remaining])


def receipt_metrics(receipts, lanes, start):
    counts = Counter(lanes.values())
    results = {}
    for lane in ("normal", "critical"):
        times = sorted((at - start) * 1000 for key, at in receipts if lanes[key] == lane)
        results[lane] = {
            "expected": counts[lane],
            "received": len(times),
            "p99_ms": round(times[math.ceil(0.99 * len(times)) - 1], 2) if times else None,
        }
    return results


async def run_cell(dsn, mode, workers, workload):
    provider = LocalSMS()
    providers = {"sms": provider}
    done = asyncio.Event()

    async def drain(db):
        semaphores = {"sms": asyncio.Semaphore(CHANNEL_LIMIT)}
        while not done.is_set():
            if mode == "baseline":
                progressed = await worker.deliver_one(db, providers, ["sms"])
            else:
                progressed = await worker.deliver_batch(
                    db, providers, ["sms"], batch_size=BATCH_SIZE, semaphores=semaphores
                )
            if len(provider.receipts) >= NOTICES:
                done.set()
            elif not progressed:
                await wait_or_stop(done, 0.15)

    async with benchmark_schema(dsn, workload) as (scoped, observer), AsyncExitStack() as stack:
        pools = []
        for _ in range(workers):
            db = await stack.enter_async_context(
                AsyncConnectionPool(scoped, min_size=1, max_size=10, configure=configure_connection, open=False)
            )
            stack.push_async_callback(close_settings, db)
            await db.wait()
            await prepare_settings(db)
            await get_settings(db)
            pools.append(db)

        due = await observer.execute(
            """select id::text, case when priority=1 then 'critical' else 'normal' end
               from notifications where state='pending' and next_attempt_at<=now()"""
        )
        lanes = dict(await due.fetchall())
        critical = NOTICES // CRITICAL_EVERY if workload == "mixed" else 0
        if len(lanes) != NOTICES or Counter(lanes.values())["critical"] != critical:
            raise RuntimeError("Seeded due notifications differ from the requested priority mix.")
        samples = [[0, len(lanes)]]
        finished = asyncio.Event()
        async with asyncio.timeout(300):
            start = perf_counter()
            monitor = None
            if workload == "mixed":
                monitor = asyncio.create_task(sample_backlog(observer, start, finished, samples))
            try:
                async with asyncio.TaskGroup() as group:
                    for db in pools:
                        group.create_task(drain(db))
                elapsed = perf_counter() - start
            finally:
                finished.set()
                if monitor is not None:
                    await monitor
        if workload == "mixed":
            final_backlog = await pending(observer)
            samples.append([round(perf_counter() - start, 3), final_backlog])
        delivered, attempts = await verify(pools[0], provider.receipts)
        result = {
            "mode": mode,
            "workload": workload,
            "workers": workers,
            "notices": NOTICES,
            "teams": 1,
            "provider_delay_ms": DELAY * 1000,
            "batch_size": 1 if mode == "baseline" else BATCH_SIZE,
            "per_worker_channel_limit": 1 if mode == "baseline" else CHANNEL_LIMIT,
            "provider_calls": len(provider.receipts),
            "unique_delivered": delivered,
            "delivery_attempts": attempts,
            "peak_in_flight": provider.peak,
            "elapsed_s": round(elapsed, 3),
            "sends_per_s": round(delivered / elapsed, 2),
        }
        if workload == "mixed":
            manifest = "\n".join(f"{key},{lane}" for key, lane in sorted(lanes.items()))
            result.update(
                workload_sha256=hashlib.sha256(manifest.encode()).hexdigest(),
                receipts=receipt_metrics(provider.receipts, lanes, start),
                backlog={
                    "start": samples[0][1], "max": max(count for _, count in samples),
                    "end": samples[-1][1], "samples_s_remaining": samples,
                },
            )
        return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("baseline", "batch"), required=True)
    parser.add_argument("--workload", choices=("uniform", "mixed"), default="uniform")
    parser.add_argument("--workers", nargs="+", type=int, default=[1, 4])
    args = parser.parse_args()
    if any(n < 1 for n in args.workers):
        parser.error("--workers must contain positive counts")
    if args.mode == "baseline" and hasattr(worker, "deliver_batch"):
        parser.error("Set PYTHONPATH to the unmodified baseline backend for --mode baseline.")
    if args.mode == "batch" and not hasattr(worker, "deliver_batch"):
        parser.error("Set PYTHONPATH to the backend with deliver_batch for --mode batch.")
    dsn = local_dsn()
    for n in args.workers:
        print(json.dumps(await run_cell(dsn, args.mode, n, args.workload)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
