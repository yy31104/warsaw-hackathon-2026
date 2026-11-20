"""Pick due open alerts in bounded batches. The row lock also serializes incoming occurrences."""

import asyncio
from datetime import timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, FixedClock, clock_for_event
from alerting.db import close_pool, open_pool, pool
from alerting.routing.throttle import lock_rules
from alerting.rules.engine import duration
from alerting.rules.storage import (
    alert_changed,
    audit,
    close_settings,
    delivery_plan,
    enqueue,
    get_settings,
)


def start_recovery(row, event_id, at, interval):
    """Keep the first confirming event and deadline throughout a stable period."""
    if row.get("resolve_pending_at") is None or row.get("resolve_episode") != row.get("latest_episode"):
        row.update(
            resolve_pending_at=at,
            resolve_due_at=at + interval,
            resolve_episode=row.get("latest_episode"),
            resolve_event_id=event_id,
        )


def cancel_recovery(row):
    row.update(resolve_pending_at=None, resolve_due_at=None, resolve_episode=None, resolve_event_id=None)


async def finish_recovery(cur, row, now):
    # The scheduler holds the same alert row lock as the event worker and the acknowledge endpoint.
    if row["resolve_event_id"] is None or row["resolve_episode"] != row["latest_episode"]:
        await cur.execute(
            """update alerts set resolve_pending_at=null,resolve_due_at=null,
               resolve_episode=null,resolve_event_id=null where id=%s""",
            (row["id"],),
        )
        return False
    await cur.execute(
        """update alerts set status='resolved',resolved_by='system',resolved_at=%s,
           resolve_reason='stable_for',next_notify_at=null,resolve_pending_at=null,
           resolve_due_at=null,resolve_episode=null,resolve_event_id=null,updated_at=%s
           where id=%s returning *""",
        (now, now, row["id"]),
    )
    updated = await cur.fetchone()
    detail = {
        "transition": "resolved",
        "cause": "stable_for",
        "episode": row["resolve_episode"],
        "pending_since": row["resolve_pending_at"].isoformat(),
        "event_id": row["resolve_event_id"],
    }
    await cur.execute(
        """insert into decisions(event_id,rule_id,rule_version,outcome,reason,alert_id)
           values(%s,%s,%s,'RESOLVED',%s,%s)""",
        (row["resolve_event_id"], row["rule_id"], row["rule_version"], Jsonb(detail), row["id"]),
    )
    await audit(cur, "system", "alert.resolve", "alert", row["id"], detail)
    await alert_changed(cur, updated)
    return True


async def tick(db, settings=None, clock=SYSTEM):
    if settings is None:
        settings = await get_settings(db)
    rules = {r["id"]: r for r, _ in settings.ruleset.rules}
    now = clock.now()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await lock_rules(cur, rules.values())
        await cur.execute(
            """select a.*,e.fields as event_fields,e.trace,e.schema_id from alerts a left join events e
            on e.id=a.representative_event_id where
            (a.resolve_due_at<=%s and a.status<>'resolved') or (a.next_notify_at<=%s and a.status='open')
            order by least(a.next_notify_at,a.resolve_due_at),a.id limit 200 for update of a skip locked""",
            (now, now),
        )
        rows = await cur.fetchall()
        for row in rows:
            if row["resolve_due_at"] is not None and row["resolve_due_at"] <= now:
                if await finish_recovery(cur, row, now):
                    continue
            if row["status"] != "open" or row["next_notify_at"] is None or row["next_notify_at"] > now:
                continue
            rule = rules.get(row["rule_id"])
            next_due = None
            group = (rule or {}).get("group", {})
            digest = group.get("mode") == "digest"
            if rule and row["event_fields"] is not None and (row["count_since_notify"] or not digest):
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
