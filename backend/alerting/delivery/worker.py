"""Claim and drain bounded delivery batches. Lease ownership fences late responses."""

import argparse
import asyncio
import os
from datetime import timedelta
from random import uniform
from time import monotonic
from uuid import uuid4

import httpx
from psycopg.rows import dict_row

from alerting.clock import SYSTEM
from alerting.contracts import RenderedMessage
from alerting.db import close_pool, open_pool, pool
from alerting.delivery.fair import next_notifications
from alerting.delivery.guard import can_dispatch, recheck_dispatch
from alerting.delivery.providers import registry
from alerting.delivery.shutdown import termination_event, wait_or_stop
from alerting.rules.storage import close_settings, emit


def exhausted(row, now):
    delay = row["rendered"]["meta"].get("due_in", 0) if row["kind"] == "throttle_summary" else 0
    deadline = row["created_at"] + timedelta(seconds=delay, minutes=15)
    return row["attempts"] >= 8 or now >= deadline


async def claim_batch(db, channels, clock, stop=None, *, batch_size=25):
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    now = clock.now()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = await next_notifications(cur, channels, now, limit=batch_size)
        if not rows or (stop is not None and stop.is_set()):
            return []
        candidates = []
        for row in rows:
            if row["state"] == "sending":
                await cur.execute(
                    """update delivery_attempts set outcome='unknown',finished_at=%s,error='lease expired'
                    where notification_id=%s and attempt=%s and finished_at is null""",
                    (now, row["id"], row["attempts"]),
                )
            if not await can_dispatch(cur, row):
                continue
            if exhausted(row, now):
                await cur.execute(
                    "update notifications set state='failed',locked_by=null,locked_until=null where id=%s",
                    (row["id"],),
                )
                await emit(
                    cur, "notifications", "notification.updated", id=row["id"],
                    team_id=row["team_id"], state="failed", attempts=row["attempts"],
                )
                continue
            candidates.append(row)

        budgets = {}
        for channel in sorted({row["channel"] for row in candidates}):
            budgets[channel] = await channel_budget(cur, channel, now, batch_size)
        claimed = []
        for row in candidates:
            channel = row["channel"]
            if budgets[channel]:
                budgets[channel] -= 1
                claimed.append(row)
        if not claimed:
            return []

        # A token identifies this claim. Completion checks it together with the notification ID.
        token = str(uuid4())
        now = clock.now()
        locked_until = now + timedelta(seconds=30)
        await cur.execute(
            """with leased as (
                update notifications set state='sending',locked_until=%s,locked_by=%s,attempts=attempts+1
                where id=any(%s) returning id,attempts
            )
            insert into delivery_attempts(notification_id,attempt,started_at)
            select id,attempts,%s from leased""",
            (locked_until, token, [row["id"] for row in claimed], now),
        )
        async with conn.pipeline():
            for row in claimed:
                row.update(locked_by=token, locked_until=locked_until, attempts=row["attempts"] + 1)
                await emit(
                    cur, "notifications", "notification.updated", id=row["id"],
                    team_id=row["team_id"], state="sending", attempts=row["attempts"],
                )
        return claimed


async def channel_budget(cur, channel, now, batch_size):
    # One row may probe a half-open circuit during its retry interval.
    await cur.execute(
        """update delivery_breakers set probe_after=%s where channel=%s
        and opened_at is not null and probe_after<=%s returning channel""",
        (now + timedelta(seconds=10), channel, now),
    )
    if await cur.fetchone():
        return 1
    await cur.execute(
        "select 1 from delivery_breakers where channel=%s and probe_after>%s",
        (channel, now),
    )
    return 0 if await cur.fetchone() else batch_size


async def claim(db, channels, clock, stop=None):
    rows = await claim_batch(db, channels, clock, stop, batch_size=1)
    return rows[0] if rows else None


async def deliver_batch(
    db, providers, channels, clock=SYSTEM, stop=None, *, batch_size=25, semaphores=None
):
    from alerting.delivery.fallback import fallback_one

    if stop is not None and stop.is_set():
        return False
    rerouted = await fallback_one(db, channels, clock)
    if stop is not None and stop.is_set():
        return rerouted
    rows = await claim_batch(db, channels, clock, stop, batch_size=batch_size)
    if not rows:
        return rerouted
    if semaphores is None:
        semaphores = {channel: asyncio.Semaphore(16) for channel in channels}
    results = await asyncio.gather(
        *(
            send_claimed(db, providers[row["channel"]], row, clock, semaphores[row["channel"]])
            for row in rows
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return True


async def deliver_one(db, providers, channels, clock=SYSTEM, stop=None):
    return await deliver_batch(db, providers, channels, clock, stop, batch_size=1)


async def send_claimed(db, provider, row, clock, semaphore):
    async with semaphore:
        # A slot can wait behind earlier sends longer than the batch lease.
        if not await recheck_dispatch(db, row, clock) or clock.now() >= row["locked_until"]:
            return
        start = monotonic()
        result = await provider.send(
            RenderedMessage.model_validate(row["rendered"]), row["contact"], str(row["id"])
        )
        latency = int((monotonic() - start) * 1000)
    now = clock.now()
    state = {
        "delivered": "delivered",
        "permanent": "failed",
        "retryable": "retrying",
        "unknown": "unknown",
    }[result.outcome]
    if state != "delivered" and exhausted(row, now):
        state = "failed"
    delay = max(result.retry_after_s or 0, min(60, 2 ** row["attempts"]) * uniform(0.8, 1.2))
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """update notifications set state=%s,locked_by=null,locked_until=null,next_attempt_at=%s,
            delivered_at=%s where id=%s and locked_by=%s returning id""",
            (
                state,
                now + timedelta(seconds=delay),
                now if state == "delivered" else None,
                row["id"],
                row["locked_by"],
            ),
        )
        if not await cur.fetchone():
            return True
        await cur.execute(
            """update delivery_attempts set finished_at=%s,outcome=%s,provider_ref=%s,error=%s,latency_ms=%s
             where notification_id=%s and attempt=%s""",
            (
                now,
                result.outcome,
                result.provider_ref,
                result.error,
                latency,
                row["id"],
                row["attempts"],
            ),
        )
        if result.outcome == "delivered":
            await cur.execute("delete from delivery_breakers where channel=%s", (row["channel"],))
        elif result.outcome in ("retryable", "unknown"):
            await cur.execute(
                """insert into delivery_breakers(channel,failures) values (%s,1)
                 on conflict(channel) do update set failures=delivery_breakers.failures+1,
                 opened_at=case when delivery_breakers.failures>=2 then coalesce(delivery_breakers.opened_at,%s) end,
                 probe_after=case when delivery_breakers.failures>=2 then %s end""",
                (row["channel"], now, now + timedelta(seconds=10)),
            )
        await emit(
            cur,
            "notifications",
            "notification.updated",
            id=row["id"],
            alert_id=row["alert_id"],
            team_id=row["team_id"],
            state=state,
            attempts=row["attempts"],
        )
    return True


async def main(channels):
    batch_size = int(os.getenv("DELIVERY_BATCH", "25"))
    concurrency = int(os.getenv("DELIVERY_CONCURRENCY", "16"))
    if batch_size < 1 or concurrency < 1:
        raise ValueError("DELIVERY_BATCH and DELIVERY_CONCURRENCY must be positive")
    semaphores = {channel: asyncio.Semaphore(concurrency) for channel in channels}
    with termination_event() as stop:
        await open_pool()
        try:
            async with httpx.AsyncClient() as client:
                providers = registry(client)
                while not stop.is_set():
                    if not await deliver_batch(
                        pool(), providers, channels, stop=stop,
                        batch_size=batch_size, semaphores=semaphores,
                    ):
                        await wait_or_stop(stop, 0.15)
        finally:
            await close_settings(pool())
            await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channels", nargs="+", default=["sms", "phone", "email", "ui"])
    args = parser.parse_args()
    asyncio.run(main([c for group in args.channels for c in group.split(",")]))
