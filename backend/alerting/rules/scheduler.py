"""Pick due open alerts in bounded batches. The row lock also serializes incoming occurrences."""

import asyncio
import logging
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, FixedClock, clock_for_event
from alerting.db import close_pool, open_pool, pool
from alerting.delivery.shutdown import termination_event, wait_or_stop
from alerting.routing.directory import by_id, team_for
from alerting.routing.ladder import step_rule, steps
from alerting.routing.throttle import lock_rules
from alerting.rules.engine import duration
from alerting.rules.failures import transient
from alerting.rules.heartbeat import check_deadlines
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


def timer_policies(rules, directory):
    """Current policy applies to open incidents, including ones created before rollout."""
    teams = by_id(directory["teams"])
    policies = []
    for rule in rules.values():
        team = teams.get(team_for(rule.get("owner", ""), directory), {})
        interval = rule.get("escalate_after", team.get("escalate_after"))
        try:
            seconds = int(duration(interval).total_seconds()) if interval else 0
        except (ValueError, OverflowError) as exc:
            logging.getLogger(__name__).warning("Invalid escalate_after for rule %s: %s", rule["id"], exc)
            seconds = 0
        delays = [int(offset.total_seconds()) for _, offset in steps(rule)[1:]]
        if seconds or delays:
            policies.append({"id": rule["id"], "unacked": seconds or None, "ladder": delays})
    return policies


async def escalate(cur, row, rule, fields, settings, clock):
    team = by_id(settings.directory["teams"]).get(row["team_id"], {})
    if row["escalation_stage"] == 0:
        step = "secondary"
        secondary = team.get("oncall", {}).get("secondary")
        people = [secondary] if secondary else []
    else:
        step = "approvers"
        people = team.get("approvers", [])
    targets = [{"to": f"user:{person}", "channels": "preferences"} for person in people]
    routing = await delivery_plan(
        cur, row, "escalation", {**rule, "route": targets}, fields, settings, clock
    )
    # The route planner's generic critical fallback is not a secondary or designated approver.
    intents = [i for i in routing.intents if i.recipient in people]
    for intent in intents:
        intent.rendered.meta.update(requires_open_alert=True, escalation_step=step)
        if intent.kind == "escalation":
            intent.dedup_key = f"{row['id']}:escalation:{intent.recipient}:{intent.channel.value}:{step}"
    await enqueue(cur, intents, row["team_id"])
    await audit(cur, "system", "alert.escalate", "alert", row["id"], {
        "step": step, "targets": people, "notifications": [i.dedup_key for i in intents],
    })
    await cur.execute("update alerts set escalation_stage=escalation_stage+1 where id=%s", (row["id"],))


async def advance_ladder(cur, row, rule, fields, settings, clock):
    stage = row["ladder_stage"] + 1
    channel, _ = steps(rule)[stage]
    routing = await delivery_plan(cur, row, "ladder_step", step_rule(rule, channel), fields, settings, clock)
    for intent in routing.intents:
        intent.rendered.meta.update(ladder_stage=stage, requires_open_alert=True)
        if intent.kind == "ladder_step":
            intent.dedup_key = f"{row['id']}:ladder_step:{intent.recipient}:{channel}:{stage}"
    await enqueue(cur, routing.intents, row["team_id"])
    await audit(cur, "system", "alert.ladder_step", "alert", row["id"], {
        "stage": stage, "channel": channel, "notifications": [i.dedup_key for i in routing.intents],
    })
    await cur.execute("update alerts set ladder_stage=%s where id=%s", (stage, row["id"]))


async def tick(db, settings=None, clock=SYSTEM, stop=None):
    if stop is not None and stop.is_set():
        return 0
    if settings is None:
        settings = await get_settings(db)
    rules = {r["id"]: r for r, _ in settings.ruleset.rules}
    now = clock.now()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await lock_rules(cur, rules.values())
        if stop is not None and stop.is_set():
            return 0
        missed = await check_deadlines(cur, settings, clock)
        if stop is not None and stop.is_set():
            return missed
        await cur.execute(
            """select a.*,e.fields as event_fields,e.trace,e.schema_id,t.unacked_due,t.ladder_due
               from alerts a left join events e on e.id=a.representative_event_id
               left join jsonb_to_recordset(%s) as p(id text,unacked bigint,ladder bigint[])
               on p.id=a.rule_id
               cross join lateral (select coalesce(a.first_seen_at,a.created_at) as opened) anchor
               cross join lateral (select
                 case when a.escalation_stage<2 then anchor.opened +
                   (a.escalation_stage+1)*p.unacked*interval '1 second' end as unacked_due,
                 anchor.opened + p.ladder[a.ladder_stage+1]*interval '1 second' as ladder_due) t
               where (a.status<>'resolved' and a.resolve_due_at<=%s)
                  or (a.status='open' and least(a.next_notify_at,t.unacked_due,t.ladder_due)<=%s)
               order by least(a.next_notify_at,a.resolve_due_at,t.unacked_due,t.ladder_due),a.id
               limit 200 for update of a skip locked""",
            (Jsonb(timer_policies(rules, settings.directory)), now, now),
        )
        rows = await cur.fetchall()
        for row in rows:
            if row["resolve_due_at"] is not None and row["resolve_due_at"] <= now:
                if await finish_recovery(cur, row, now):
                    continue
            if row["status"] != "open":
                continue
            rule = rules.get(row["rule_id"])
            fields = {**(row["event_fields"] or {}), "gen_sent_at": (row["trace"] or {}).get("gen_sent_at")}
            if row["unacked_due"] is not None and row["unacked_due"] <= now:
                await escalate(cur, row, rule, fields, settings, clock)
            if row["ladder_due"] is not None and row["ladder_due"] <= now:
                await advance_ladder(cur, row, rule, fields, settings, clock)
            if row["next_notify_at"] is None or row["next_notify_at"] > now:
                continue
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
                    fields,
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
        return missed + len(rows)


async def main():
    with termination_event() as stop:
        await open_pool()
        try:
            while not stop.is_set():
                try:
                    await tick(pool(), stop=stop)
                except psycopg.Error as exc:
                    if not transient(exc):
                        raise
                    logging.getLogger(__name__).warning("Transient scheduler failure; retrying", exc_info=True)
                await wait_or_stop(stop, 2)
        finally:
            await close_settings(pool())
            await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
