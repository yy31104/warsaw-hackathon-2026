"""Pick due open alerts in bounded batches. The row lock also serializes incoming occurrences."""

import asyncio
from datetime import timedelta

from psycopg.rows import dict_row

from alerting.clock import SYSTEM, FixedClock, clock_for_event
from alerting.db import close_pool, open_pool, pool
from alerting.routing.throttle import lock_rules
from alerting.rules.engine import duration
from alerting.rules.storage import (
    alert_changed,
    close_settings,
    delivery_plan,
    enqueue,
    get_settings,
)


async def tick(db, settings=None, clock=SYSTEM):
    if settings is None:
        settings = await get_settings(db)
    rules = {r["id"]: r for r, _ in settings.ruleset.rules}
    now = clock.now()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await lock_rules(cur, rules.values())
        await cur.execute(
            """select a.*,e.fields as event_fields,e.trace from alerts a join events e
            on e.id=a.representative_event_id where a.next_notify_at<=%s and a.status='open'
            order by a.next_notify_at,a.id limit 200 for update of a skip locked""",
            (now,),
        )
        rows = await cur.fetchall()
        for row in rows:
            rule = rules.get(row["rule_id"])
            next_due = None
            group = (rule or {}).get("group", {})
            digest = group.get("mode") == "digest"
            if rule and (row["count_since_notify"] or not digest):
                transition = "digest_due" if group.get("mode") == "digest" else "cooldown_elapsed"
                effective = clock
                if row["trace"].get("demo_clock") and clock_for_event(row["trace"]) is not SYSTEM:
                    effective = FixedClock(
                        max(row["last_seen_at"], row["last_notified_at"] or row["last_seen_at"])
                        + max(now - row["updated_at"], timedelta())
                    )
                routing = await delivery_plan(
                    cur,
                    row,
                    transition,
                    rule,
                    {**row["event_fields"], "gen_sent_at": row["trace"].get("gen_sent_at")},
                    settings,
                    effective,
                )
                await enqueue(cur, routing.intents, row["team_id"])
                if routing.intents:
                    await cur.execute(
                        "update alerts set count_since_notify=0,last_notified_at=%s where id=%s",
                        (effective.now(), row["id"]),
                    )
                    if not digest:
                        next_due = now + duration(group.get("cooldown", "10m"))
                    await alert_changed(cur, row)
            await cur.execute(
                "update alerts set next_notify_at=%s,updated_at=now() where id=%s",
                (next_due, row["id"]),
            )
        return len(rows)


async def main():
    await open_pool()
    try:
        while True:
            await tick(pool())
            await asyncio.sleep(2)
    finally:
        await close_settings(pool())
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
