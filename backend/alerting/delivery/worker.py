"""Durably claim one delivery per available sender. Lease ownership fences late responses."""
import argparse
import asyncio
from datetime import timedelta
from random import uniform
from time import monotonic
from uuid import uuid4

import httpx
from psycopg.rows import dict_row

from alerting.clock import SYSTEM
from alerting.contracts import RenderedMessage
from alerting.db import pool, open_pool, close_pool
from alerting.delivery.providers import registry
from alerting.rules.storage import emit


async def claim(db, channels, clock):
    now = clock.now()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("""select n.*, a.team_id from notifications n join alerts a on a.id=n.alert_id
            left join delivery_breakers b on b.channel=n.channel
            where n.channel=any(%s) and (b.probe_after is null or b.probe_after<=%s)
            and ((n.state in ('pending','retrying','unknown') and n.next_attempt_at<=%s)
            or (n.state='sending' and n.locked_until<=%s))
            order by n.priority,n.next_attempt_at,n.id limit 1 for update of n skip locked""",
            (list(channels), now, now, now))
        row = await cur.fetchone()
        if not row:
            return None
        if row["state"] == "sending":
            await cur.execute("""update delivery_attempts set outcome='unknown',finished_at=%s,error='lease expired'
                where notification_id=%s and attempt=%s and finished_at is null""", (now, row["id"], row["attempts"]))
        if row["attempts"] >= 8 or now-row["created_at"] >= timedelta(minutes=15):
            await cur.execute("update notifications set state='failed',locked_by=null,locked_until=null where id=%s", (row["id"],))
            await emit(cur, "notifications", "notification.updated", id=row["id"], team_id=row["team_id"],
                       state="failed", attempts=row["attempts"])
            return None
        row.update(locked_by=str(uuid4()), attempts=row["attempts"]+1)
        await cur.execute("""update notifications set state='sending',locked_until=%s,locked_by=%s,attempts=%s
            where id=%s""", (now+timedelta(seconds=30), row["locked_by"], row["attempts"], row["id"]))
        await cur.execute("insert into delivery_attempts(notification_id,attempt,started_at) values (%s,%s,%s)",
                          (row["id"], row["attempts"], now))
        await emit(cur, "notifications", "notification.updated", id=row["id"], team_id=row["team_id"],
                   state="sending", attempts=row["attempts"])
        return row


async def deliver_one(db, providers, channels, clock=SYSTEM):
    row = await claim(db, channels, clock)
    if not row:
        return False
    start = monotonic()
    result = await providers[row["channel"]].send(RenderedMessage.model_validate(row["rendered"]),
                                                  row["contact"], str(row["id"]))
    latency = int((monotonic()-start)*1000)
    now = clock.now()
    state = {"delivered": "delivered", "permanent": "failed", "retryable": "retrying", "unknown": "unknown"}[result.outcome]
    if state != "delivered" and (row["attempts"] >= 8 or now-row["created_at"] >= timedelta(minutes=15)):
        state = "failed"
    delay = max(result.retry_after_s or 0, min(60, 2**row["attempts"]) * uniform(.8, 1.2))
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("""update notifications set state=%s,locked_by=null,locked_until=null,next_attempt_at=%s,
            delivered_at=%s where id=%s and locked_by=%s returning id""", (state, now+timedelta(seconds=delay),
            now if state=="delivered" else None, row["id"], row["locked_by"]))
        if not await cur.fetchone():
            return True
        await cur.execute("""update delivery_attempts set finished_at=%s,outcome=%s,provider_ref=%s,error=%s,latency_ms=%s
             where notification_id=%s and attempt=%s""", (now,result.outcome,result.provider_ref,result.error,
             latency,row["id"],row["attempts"]))
        if result.outcome == "delivered":
            await cur.execute("delete from delivery_breakers where channel=%s", (row["channel"],))
        elif result.outcome in ("retryable", "unknown"):
            await cur.execute("""insert into delivery_breakers(channel,failures) values (%s,1)
                 on conflict(channel) do update set failures=delivery_breakers.failures+1,
                 opened_at=case when delivery_breakers.failures>=2 then coalesce(delivery_breakers.opened_at,%s) end,
                 probe_after=case when delivery_breakers.failures>=2 then %s end""",
                 (row["channel"],now,now+timedelta(seconds=10)))
        await emit(cur, "notifications", "notification.updated", id=row["id"], alert_id=row["alert_id"],
                   team_id=row["team_id"], state=state, attempts=row["attempts"])
    return True


async def main(channels):
    await open_pool()
    try:
        async with httpx.AsyncClient() as client:
            providers = registry(client)
            while True:
                if not await deliver_one(pool(), providers, channels):
                    await asyncio.sleep(.15)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channels", nargs="+", default=["sms", "phone", "email", "ui"])
    args = parser.parse_args()
    asyncio.run(main([c for group in args.channels for c in group.split(",")]))
