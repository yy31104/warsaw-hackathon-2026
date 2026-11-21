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
from alerting.routing.throttle import lock_rules, scope_to_open_alert
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


def confirmed_recovery(row):
    return row["resolve_event_id"] is not None and row["resolve_episode"] == row["latest_episode"]


async def load_team_silences(conn, at, *, team=None):
    try:
        from alerting.rules.silences import load_active
    except ModuleNotFoundError as exc:
        if exc.name != "alerting.rules.silences":
            raise
        return {}
    windows = {}
    for silence in await load_active(conn, at, team=team):
        windows.setdefault(silence.team_id, []).append(silence)
    return windows


def timer_silence(windows, row, fields, at):
    if team_windows := windows.get(row["team_id"]):
        from alerting.rules.silences import active_silence

        return active_silence(team_windows, fields, at, severity=row["severity"])
    return None


async def finish_recovery(cur, row, now):
    # The scheduler holds the same alert row lock as the event worker and the acknowledge endpoint.
    if not confirmed_recovery(row):
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
        owner = rule.get("owner")
        team = teams.get(team_for(owner, directory), {}) if isinstance(owner, str) else {}
        interval = rule.get("escalate_after", team.get("escalate_after"))
        try:
            seconds = int(duration(interval).total_seconds()) if interval else 0
        except (ValueError, OverflowError) as exc:
            logging.getLogger(__name__).warning("Invalid escalate_after for rule %s: %s", rule["id"], exc)
            seconds = 0
        delays = [int(offset.total_seconds()) for _, offset in steps(rule)[1:]]
        if seconds or delays:
            policies.append({
                "id": rule["id"], "unacked": seconds or None, "ladder": delays,
                "digest": bool(delays) and (rule.get("group") or {}).get("mode") == "digest",
            })
    return policies


# Used by both the scheduler's batch claim and the read-only alert detail.
TIMER_ANCHORS = """
    left join events recovery on recovery.id=a.resolve_event_id
    left join lateral (
      select d.created_at as received,(d.reason->>'eligible_at')::timestamptz as eligible_at
      from decisions d where p.id is not null and d.alert_id=a.id
        and d.reason->>'notification_cause'='first_eligible_after_silence'
      order by d.id limit 1) first_allowed on true
    cross join lateral (select
      extract(epoch from case
        when a.occurrence_count>0 and a.last_notified_at is null and a.count_since_notify=0 then null
        when %(live)s then coalesce(first_allowed.received,a.created_at)
        else coalesce(first_allowed.eligible_at,a.first_seen_at,a.created_at) end) as opened_epoch,
      case when %(live)s and recovery.trace ? 'demo_clock'
        then recovery.received_at + (a.resolve_due_at-a.resolve_pending_at)
        else a.resolve_due_at end as recovery_due) anchor
    cross join lateral (select case when a.status='open' and a.escalation_stage<2 then
      anchor.opened_epoch + (a.escalation_stage+1)*p.unacked end as unacked_at) escalation
"""


async def escalation_plan(cur, row, rule, fields, settings, clock, *, reserve_throttle):
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
        cur, row, "escalation", {**rule, "route": targets}, fields, settings, clock,
        reserve_throttle=reserve_throttle,
    )
    # The route planner's generic critical fallback is not a secondary or designated approver.
    return step, people, [i for i in routing.intents if i.recipient in people]


async def alert_with_next_escalation(cur, alert_id, settings, clock=SYSTEM):
    rules = {r["id"]: r for r, _ in settings.ruleset.rules}
    await cur.execute(
        f"""select a.*,e.fields as event_fields,e.trace,e.schema_id,anchor.recovery_due,
                   to_timestamp(escalation.unacked_at) as escalation_at
            from alerts a left join events e on e.id=a.representative_event_id
            left join jsonb_to_recordset(%(policies)s) as p(id text,unacked bigint) on p.id=a.rule_id
            {TIMER_ANCHORS}
            where a.id=%(id)s""",
        {"id": alert_id, "policies": Jsonb(timer_policies(rules, settings.directory)), "live": clock is SYSTEM},
    )
    row = await cur.fetchone()
    if row is None:
        return None
    row["next_escalation"] = None
    at = row["escalation_at"]
    if at is None:
        return row
    fields = {**(row["event_fields"] or {}), "gen_sent_at": (row["trace"] or {}).get("gen_sent_at")}
    windows = await load_team_silences(cur.connection, at, team=row["team_id"])
    # Keep the original release time for a delayed poll, and respect any current hold on an overdue step.
    for check_at in (at, clock.now()):
        planning_at = max(at, check_at)
        while silence := timer_silence(windows, row, fields, planning_at):
            at = planning_at = silence.expires_at
    if row["recovery_due"] is not None and row["recovery_due"] <= planning_at and confirmed_recovery(row):
        return row
    level, _, intents = await escalation_plan(
        cur, row, rules[row["rule_id"]], fields, settings, FixedClock(planning_at), reserve_throttle=False,
    )
    row["next_escalation"] = {
        "at": at, "level": level, "targets": sorted({i.recipient for i in intents}),
        "channels": sorted({i.channel.value for i in intents}),
    }
    return row


async def escalate(cur, row, rule, fields, settings, clock):
    step, people, intents = await escalation_plan(cur, row, rule, fields, settings, clock, reserve_throttle=True)
    for intent in intents:
        scope_to_open_alert(intent)
        intent.rendered.meta["escalation_step"] = step
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
        scope_to_open_alert(intent)
        intent.rendered.meta["ladder_stage"] = stage
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
        windows = await load_team_silences(conn, now)
        if stop is not None and stop.is_set():
            return missed
        active_ids = [str(w.id) for team in windows.values() for w in team if w.created_at <= now]
        await cur.execute(
            f"""select a.*,e.fields as event_fields,e.trace,e.schema_id,anchor.recovery_due,
                 escalation.unacked_at<=cutoff.epoch as escalate,t.ladder_at<=cutoff.epoch as advance
               from alerts a
               left join events e on e.id=a.representative_event_id
               left join jsonb_to_recordset(%(policies)s) as p(id text,unacked bigint,ladder bigint[],digest bool)
                 on p.id=a.rule_id
               cross join (select extract(epoch from %(now)s::timestamptz) as epoch) cutoff
               {TIMER_ANCHORS}
               left join lateral (select extract(epoch from
                     (n.rendered#>>'{{meta,ladder_started_at}}')::timestamptz) as started_epoch
                 from notifications n where p.digest and a.status='open' and a.last_notified_at is not null
                   and p.ladder[a.ladder_stage+1] is not null and n.alert_id=a.id
                   and n.rendered->'meta' ? 'ladder_started_at'
                 order by n.created_at,n.id limit 1) first_digest on true
               cross join lateral (select
                 (case when p.digest then first_digest.started_epoch else anchor.opened_epoch end)
                   + p.ladder[a.ladder_stage+1] as ladder_at) t
               where (a.status<>'resolved' and anchor.recovery_due<=%(now)s)
                  or (a.status='open' and least(extract(epoch from a.next_notify_at),
                       escalation.unacked_at,t.ladder_at)<=cutoff.epoch
                      and (cardinality(%(silences)s::text[])=0 or not exists (
                        select 1 from audit_log l where l.target_kind='alert' and l.target_id=a.id::text
                          and l.action='alert.timer_silenced' and l.detail->>'silence_id'=any(%(silences)s)
                          and l.detail @> jsonb_build_object('event_id',a.representative_event_id,
                                                             'severity',a.severity,'team_id',a.team_id))))
               order by least(extract(epoch from a.next_notify_at),extract(epoch from anchor.recovery_due),
                       escalation.unacked_at,t.ladder_at),a.id
               limit 200 for update of a skip locked""",
            {"policies": Jsonb(timer_policies(rules, settings.directory)), "live": clock is SYSTEM,
             "now": now, "silences": active_ids},
        )
        rows = await cur.fetchall()
        for row in rows:
            if row["recovery_due"] is not None and row["recovery_due"] <= now:
                if await finish_recovery(cur, row, now):
                    continue
            if row["status"] != "open":
                continue
            rule = rules.get(row["rule_id"])
            fields = {**(row["event_fields"] or {}), "gen_sent_at": (row["trace"] or {}).get("gen_sent_at")}
            if rule and (silence := timer_silence(windows, row, fields, now)):
                # Skip this same evidence on later polls until the window expires or is revoked.
                await audit(cur, "system", "alert.timer_silenced", "alert", row["id"], {
                    "silence_id": silence.id, "event_id": row["representative_event_id"],
                    "severity": row["severity"], "team_id": row["team_id"],
                })
                continue
            if row["escalate"]:
                await escalate(cur, row, rule, fields, settings, clock)
            if row["advance"]:
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
                if digest:
                    for intent in routing.intents:
                        if intent.rendered.meta.get("ladder_stage") == 0:
                            intent.rendered.meta["ladder_started_at"] = now.isoformat()
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
