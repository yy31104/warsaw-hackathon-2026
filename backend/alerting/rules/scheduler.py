"""Pick due open alerts in bounded batches. The row lock also serializes incoming occurrences."""
import asyncio
from psycopg.rows import dict_row

from alerting.clock import SYSTEM, FixedClock, clock_for_event
from alerting.db import pool, open_pool, close_pool
from alerting.routing import plan
from alerting.rules.storage import Settings, enqueue, alert_changed


async def tick(db, settings, clock=SYSTEM):
    rules = {r["id"]: r for r, _ in settings.ruleset.rules}
    now = clock.now()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("""select a.*,e.fields as event_fields,e.trace from alerts a join events e
            on e.id=a.representative_event_id where a.next_notify_at<=%s and a.status='open'
            order by a.next_notify_at,a.id limit 200 for update of a skip locked""", (now,))
        rows = await cur.fetchall()
        for row in rows:
            rule = rules.get(row["rule_id"])
            if rule and row["count_since_notify"]:
                group = rule.get("group",{})
                transition = "digest_due" if group.get("mode")=="digest" else "cooldown_elapsed"
                effective = clock
                if row["trace"].get("demo_clock") and clock_for_event(row["trace"]) is not SYSTEM:
                    effective = FixedClock(row["last_seen_at"] + max(now-row["updated_at"], now-now))
                routing = plan(row,transition,rule,{**row["event_fields"],"gen_sent_at":row["trace"].get("gen_sent_at")},
                               settings.directory,settings.runbooks,{},effective)
                await enqueue(cur,routing.intents,row["team_id"])
                if routing.intents:
                    await cur.execute("update alerts set count_since_notify=0,last_notified_at=%s where id=%s",
                                      (effective.now(),row["id"]))
                    await alert_changed(cur,row)
            await cur.execute("update alerts set next_notify_at=null,updated_at=now() where id=%s",(row["id"],))
        return len(rows)


async def main():
    await open_pool()
    try:
        settings = Settings.load()
        while True:
            await tick(pool(),settings)
            await asyncio.sleep(2)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
