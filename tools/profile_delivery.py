"""Measure the unmodified, single-row delivery worker using bench_delivery's uniform workload.

Set DATABASE_URL to a disposable local PostgreSQL database, then run from the new checkout:

    PYTHONPATH=/tmp/delivery-before/backend backend/.venv/bin/python -B tools/profile_delivery.py --workers 1 4

Times are sums of elapsed worker time and may overlap across workers. A transaction
measurement starts after checkout and ends after commit and pool return. Pool wait is
reported separately. Stage totals include their transaction and pool time.
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import ceil
from time import perf_counter
from unittest.mock import patch

import psycopg
from psycopg_pool import AsyncConnectionPool

import bench_delivery as bench
from alerting.delivery import fallback, worker


@dataclass
class Poll:
    team_lock_granted: int = 0
    team_lock_denied: int = 0

    def idle_cause(self):
        # The benchmark seeds one team and one channel. Only a denied lock without
        # a subsequent acquisition can be the direct reason for a worker's idle wait.
        if self.team_lock_denied and not self.team_lock_granted:
            return "team_lock_denied"
        if self.team_lock_granted:
            return "lock_granted_no_claim"
        return "no_team_candidate"


def summarize(samples):
    if not samples:
        return {"n": 0, "total_ms": 0}
    ordered = sorted(samples)
    total = sum(ordered) * 1000
    return {
        "n": len(ordered), "total_ms": round(total, 2), "mean_ms": round(total / len(ordered), 3),
        "p50_ms": round(1000 * ordered[ceil(len(ordered) * .5) - 1], 3),
        "p95_ms": round(1000 * ordered[ceil(len(ordered) * .95) - 1], 3),
    }


async def profile_cell(dsn, workers):
    phase = ContextVar("delivery_phase", default=None)
    poll = ContextVar("delivery_poll", default=None)
    idle_cause = ContextVar("delivery_idle_cause", default=None)
    elapsed = defaultdict(list)
    locks = Counter()
    original_claim = worker.claim
    original_deliver = worker.deliver_one
    original_fallback = fallback.fallback_one
    original_wait = bench.wait_or_stop

    class TimedCursor(psycopg.AsyncCursor):
        async def execute(self, query, params=None, **kwargs):
            self.team_lock_phase = None
            stage = phase.get()
            if stage in ("claim", "fallback") and isinstance(query, str):
                if "group by n.priority,coalesce(a.team_id,'')" in query:
                    start = perf_counter()
                    result = await super().execute(query, params, **kwargs)
                    elapsed[stage + "_team_discovery_sql"].append(perf_counter() - start)
                    return result
                if ("pg_try_advisory_xact_lock" in query and params and isinstance(params[0], str)
                        and params[0].startswith('["delivery-team",')):
                    self.team_lock_phase = stage
                    self.team_lock_started = perf_counter()
            return await super().execute(query, params, **kwargs)

        async def fetchone(self):
            row = await super().fetchone()
            if self.team_lock_phase:
                granted = row["acquired"]
                locks[self.team_lock_phase, granted] += 1
                label = "granted" if granted else "denied"
                elapsed[f"{self.team_lock_phase}_lock_{label}_sql"].append(perf_counter() - self.team_lock_started)
                if self.team_lock_phase == "claim":
                    if granted:
                        poll.get().team_lock_granted += 1
                    else:
                        poll.get().team_lock_denied += 1
                self.team_lock_phase = None
            return row

    class TimedPool(AsyncConnectionPool):
        def __init__(self, *args, **kwargs):
            kwargs["kwargs"] = {**(kwargs.get("kwargs") or {}), "cursor_factory": TimedCursor}
            super().__init__(*args, **kwargs)

        @asynccontextmanager
        async def connection(self, timeout=None):
            stage = phase.get()
            start = perf_counter()
            async with super().connection(timeout) as conn:
                acquired = perf_counter()
                if stage:
                    elapsed[stage + "_pool_wait"].append(acquired - start)
                yield conn
            if stage:
                elapsed[stage + "_tx"].append(perf_counter() - acquired)

    class TimedSMS(bench.LocalSMS):
        async def send(self, *args):
            start = perf_counter()
            result = await super().send(*args)
            elapsed["provider"].append(perf_counter() - start)
            return result

    async def timed_fallback(*args, **kwargs):
        token = phase.set("fallback")
        start = perf_counter()
        try:
            return await original_fallback(*args, **kwargs)
        finally:
            elapsed["fallback_total"].append(perf_counter() - start)
            phase.reset(token)

    async def timed_claim(*args, **kwargs):
        token = phase.set("claim")
        start = perf_counter()
        try:
            row = await original_claim(*args, **kwargs)
            elapsed["claim_hit" if row else "claim_miss"].append(perf_counter() - start)
            return row
        finally:
            phase.reset(token)

    async def timed_delivery(*args, **kwargs):
        observation = Poll()
        active = poll.set(observation)
        stage = phase.set("finalize")
        try:
            progressed = await original_deliver(*args, **kwargs)
            idle_cause.set(None if progressed else observation.idle_cause())
            return progressed
        finally:
            phase.reset(stage)
            poll.reset(active)

    async def timed_idle(done, timeout):
        cause = idle_cause.get()
        if cause is None or timeout != 0.15:
            raise RuntimeError("Benchmark idle-wait behavior changed")
        start = perf_counter()
        try:
            return await original_wait(done, timeout)
        finally:
            elapsed["idle_" + cause].append(perf_counter() - start)
            idle_cause.set(None)

    with ExitStack() as patches:
        for target, name, instrumented in (
            (bench, "wait_or_stop", timed_idle), (bench, "AsyncConnectionPool", TimedPool),
            (bench, "LocalSMS", TimedSMS), (worker, "deliver_one", timed_delivery),
            (worker, "claim", timed_claim), (fallback, "fallback_one", timed_fallback),
        ):
            patches.enter_context(patch.object(target, name, instrumented))
        results = await bench.run_cell(dsn, "baseline", workers, "uniform")
    sent = results["unique_delivered"]
    if not (len(elapsed["claim_hit"]) == len(elapsed["provider"]) == len(elapsed["finalize_tx"]) == sent):
        raise RuntimeError("Measured claim, handoff and finalization counts differ from reconciliation")
    for cause in ("team_lock_denied", "lock_granted_no_claim", "no_team_candidate"):
        elapsed["idle_" + cause]
    results["timings"] = {name: summarize(values) for name, values in sorted(elapsed.items())}
    results["team_lock_results"] = {
        phase_name: {"granted": locks[phase_name, True], "denied": locks[phase_name, False]}
        for phase_name in ("claim", "fallback")
    }
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", nargs="+", type=int, default=[1, 4])
    args = parser.parse_args()
    if any(n < 1 for n in args.workers) or hasattr(worker, "deliver_batch"):
        parser.error("Use positive worker counts and set PYTHONPATH to the single-row baseline backend.")
    dsn = bench.local_dsn()
    for n in args.workers:
        print(json.dumps(await profile_cell(dsn, n)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
