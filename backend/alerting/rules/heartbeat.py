"""The inbox worker records positive evidence; the scheduler claims missed deadlines."""

from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, clock_for_event
from alerting.routing.directory import team_for
from alerting.rules.engine import duration, grouping
from alerting.rules.failures import evaluating
from alerting.rules.scoring import score
from alerting.rules.storage import alert_changed, audit, delivery_plan, enqueue
from alerting.templates import lookup, render


def heartbeat_deadline(expect, schema, fields, seen):
    paths = expect["key"]
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("expect.key must be a list of field paths")
    interval = duration(expect["every"])
    if interval.total_seconds() <= 0:
        raise ValueError("expect.every must be positive")
    key, values = grouping({"group": {"by": paths}}, schema, fields) if paths else ("", {})
    return key, values, seen + interval


async def record_heartbeats(cur, settings, event, clock):
    """Use the ordinary compiled rule predicate, including its recorded field trace."""
    matched = False
    # A delayed batch must not make an old heartbeat look newly received.
    seen = clock.now() if clock_for_event(event.trace) is not SYSTEM else event.received_at
    for rule, predicate in settings.ruleset.rules:
        if "expect" not in rule:
            continue
        with evaluating(rule):
            ok, trace = predicate(event.fields)
            if not ok:
                continue
            matched = True
            schema = settings.ruleset.schemas.get(event.schema_id, {})
            key, values, due = heartbeat_deadline(rule["expect"], schema, event.fields, seen)
            version = rule.get("version", 1)
            await cur.execute(
                """insert into heartbeat_watches as w
                   (rule_id,alert_key,rule_version,group_values,last_seen_at,last_event_id,due_at)
                   values(%s,%s,%s,%s,%s,%s,%s) on conflict(rule_id,alert_key) do update set
                   rule_version=excluded.rule_version,group_values=excluded.group_values,
                   last_seen_at=excluded.last_seen_at,last_event_id=excluded.last_event_id,
                   due_at=excluded.due_at where (w.last_seen_at,w.last_event_id)
                   < (excluded.last_seen_at,excluded.last_event_id) returning alert_id""",
                (rule["id"], key, version, Jsonb(values), seen, event.id, due),
            )
            watch = await cur.fetchone()
            aid = watch["alert_id"] if watch else None
            detail = {"trace": trace, "alert_key": key, "last_seen_advanced": watch is not None}
            outcome = "ACCEPTED"
            if aid is not None:
                await cur.execute(
                    "select status,first_seen_at from alerts where id=%s for update", (aid,)
                )
                incident = await cur.fetchone()
                closed = incident["status"] == "resolved"
                fresh = seen >= incident["first_seen_at"] and due > clock.now()
                if fresh or closed:
                    if not closed:
                        await cur.execute(
                            """update alerts set status='resolved',resolved_by='system',resolved_at=%s,
                               resolve_reason='heartbeat_received',next_notify_at=null,updated_at=%s
                               where id=%s returning *""",
                            (seen, seen, aid),
                        )
                        updated = await cur.fetchone()
                        outcome = "RESOLVED"
                        detail.update(transition="resolved", cause="heartbeat_received")
                        await audit(cur, "system", "alert.resolve", "alert", aid, {"event_id": event.id})
                        await alert_changed(cur, updated)
                    await cur.execute(
                        "update heartbeat_watches set alert_id=null where rule_id=%s and alert_key=%s",
                        (rule["id"], key),
                    )
                else:
                    detail["cause"] = "heartbeat_predates_recovery"
                    await cur.execute(
                        "update heartbeat_watches set due_at=null where rule_id=%s and alert_key=%s",
                        (rule["id"], key),
                    )
            await cur.execute(
                """insert into decisions(event_id,rule_id,rule_version,alert_id,outcome,reason)
                   values(%s,%s,%s,%s,%s,%s)""",
                (event.id, rule["id"], version, aid, outcome, Jsonb(detail)),
            )
    return matched


async def check_deadlines(cur, settings, clock):
    rules = {r["id"]: r for r, _ in settings.ruleset.rules if "expect" in r}
    if not rules:
        return 0
    await cur.execute(
        """select w.*, e.fields, e.trace, e.environment, e.schema_id from heartbeat_watches w
           join events e on e.id=w.last_event_id
           cross join lateral (select case when %(live)s and e.trace ? 'demo_clock'
               then e.received_at + (w.due_at-w.last_seen_at) else w.due_at end as due) timer
           where w.due_at is not null and timer.due<=%(now)s and w.rule_id=any(%(rules)s)
           order by timer.due,w.rule_id,w.alert_key limit 200 for update of w skip locked""",
        {"live": clock is SYSTEM, "now": clock.now(), "rules": list(rules)},
    )
    watches = await cur.fetchall()
    for watch in watches:
        rule = rules[watch["rule_id"]]
        aid = None
        # A replacement rule starts monitoring when it receives its first matching heartbeat.
        if watch["rule_version"] == rule.get("version", 1):
            aid = await raise_missed(cur, rule, watch, settings, clock)
        await cur.execute(
            "update heartbeat_watches set due_at=null,alert_id=%s where rule_id=%s and alert_key=%s",
            (aid, rule["id"], watch["alert_key"]),
        )
    return len(watches)


async def raise_missed(cur, rule, watch, settings, clock):
    fields = watch["fields"]
    present = rule.get("present", {})
    severity = rule.get("severity", "warning")
    priority, total, parts = score(severity, watch["environment"], 1, rule)
    team = team_for(rule.get("owner", ""), settings.directory)
    sensitive = settings.ruleset.schemas.get(watch["schema_id"], {}).get("sensitive", [])
    await cur.execute(
        """insert into alerts(rule_id,rule_version,alert_key,team_id,occurrence_count,count_since_notify,
           severity,priority,score,score_breakdown,lane,first_seen_at,last_seen_at,title,body,color,
           fields,group_values,representative_event_id,sensitive_paths) values(%s,%s,%s,%s,1,1,%s,%s,%s,%s,%s,%s,%s,
           %s,%s,%s,%s,%s,%s,%s) on conflict(rule_id,alert_key) where status<>'resolved' do nothing
           returning *""",
        (
            rule["id"], watch["rule_version"], watch["alert_key"], team, severity, priority, total,
            Jsonb(parts), "critical" if severity == "critical" else "normal", watch["due_at"],
            watch["due_at"], render(present.get("title") or rule["id"], fields),
            render(present.get("body"), fields) or f"No heartbeat for {rule['expect']['every']}.",
            present.get("color"), Jsonb({p: lookup(fields, p) for p in present.get("fields", [])}),
            Jsonb(watch["group_values"]), watch["last_event_id"], sensitive,
        ),
    )
    alert = await cur.fetchone()
    if alert is None:
        # An existing open incident owns this identity even across a configuration change.
        await cur.execute(
            "select id from alerts where rule_id=%s and alert_key=%s and status<>'resolved' for update",
            (rule["id"], watch["alert_key"]),
        )
        existing = await cur.fetchone()
        return existing["id"] if existing else None
    alert["schema_id"] = watch["schema_id"]
    routing = await delivery_plan(
        cur, alert, "heartbeat_missed", rule, {**fields, "gen_sent_at": watch["trace"].get("gen_sent_at")},
        settings, clock,
    )
    await enqueue(cur, routing.intents, team)
    await cur.execute(
        """update alerts set runbook_id=%s,last_notified_at=%s,count_since_notify=%s where id=%s""",
        (routing.runbook_id, clock.now() if routing.intents else None, int(not routing.intents), alert["id"]),
    )
    detail = {
        "transition": "heartbeat_missed",
        "last_seen_at": watch["last_seen_at"].isoformat(),
        "due_at": watch["due_at"].isoformat(),
        "notifications": [i.dedup_key for i in routing.intents],
    }
    await cur.execute(
        """insert into decisions(event_id,rule_id,rule_version,alert_id,outcome,reason)
           values(%s,%s,%s,%s,'HEARTBEAT_MISSED',%s)""",
        (watch["last_event_id"], rule["id"], watch["rule_version"], alert["id"], Jsonb(detail)),
    )
    await alert_changed(cur, alert, created=True)
    return alert["id"]
