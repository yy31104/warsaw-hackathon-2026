"""One lane per process. Transaction-scoped locks serialize only overlapping rule/key pairs."""

import argparse
import asyncio
from collections import defaultdict
from copy import copy
from dataclasses import replace
from inspect import signature
import logging
import psycopg

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, FixedClock, clock_for_event, demo_mode
from alerting.contracts import SEVERITY_ORDER, CanonicalEvent
from alerting.db import close_pool, open_pool, pool
from alerting.routing import plan
from alerting.routing.event_policy import eligible_ruleset
from alerting.routing.directory import team_for
from alerting.routing.throttle import lock_rules
from alerting.rules.engine import AlertOp, duration, episode_for, evaluate, grouping
from alerting.rules.failures import BadEvent, RuleFault, evaluating, transient
from alerting.rules.heartbeat import record_heartbeats
from alerting.rules.scoring import score
from alerting.rules.scheduler import cancel_recovery, start_recovery
from alerting.rules.storage import (
    Settings,
    alert_changed,
    close_settings,
    delivery_plan,
    enqueue_event_plans,
    get_settings,
)

COLUMNS = (
    "id rule_id rule_version alert_key status severity priority lane team_id title body color fields group_values "
    "runbook_id occurrence_count count_since_notify first_seen_at last_seen_at last_notified_at "
    "representative_event_id resolved_by resolved_at resolve_reason score score_breakdown next_notify_at latest_episode "
    "resolve_pending_at resolve_due_at resolve_episode resolve_event_id sensitive_paths"
).split()

# Keep workers compatible while the spike engine and SLO scorer roll out independently.
HAS_SPIKE_STATE = hasattr(AlertOp, "spike_state")
SCORE_ACCEPTS_SCHEMA = "schema" in signature(score).parameters


class BatchState(dict):
    """Mutable open incidents and episode history projected up to this event in the batch."""

    def __init__(self, current, ruleset, settings, windows):
        super().__init__(current)
        self.resolved = {}
        self.firing_episodes = defaultdict(set)
        self.rules = {r["id"]: r for r, _ in ruleset.rules}
        self.directory = settings.directory
        self.windows = windows

    def get_resolved_episode(self, rule_id, key, episode):
        return self.resolved.get((rule_id, key, episode))

    def remember_episode(self, rule_id, key, episode, alert_id):
        if episode is not None:
            self.firing_episodes[alert_id].add(episode)

    def close_episodes(self, rule_id, key, alert_id):
        for episode in self.firing_episodes.pop(alert_id, ()):
            self.resolved[rule_id, key, episode] = alert_id

    def silences_for_rule(self, rule):
        if not self.windows:
            return ()
        team = team_for(rule.get("owner") or "", self.directory)
        return self.windows.get(team, ())

    def get_silences(self, rule_id):
        return self.silences_for_rule(self.rules[rule_id])


async def load_batch_state(cur, current, keys, candidate_episodes, ruleset, settings, at):
    """After lock_open, prefetch history for this batch's (rule, key, episode) tuples."""
    from alerting.rules import engine
    from alerting.rules.scheduler import load_team_silences

    state = BatchState(current, ruleset, settings, await load_team_silences(cur.connection, at))
    wanted = sorted((r, k, ep) for r, k, ep in candidate_episodes if ep is not None and (r, k) in keys)
    if not wanted or not hasattr(engine, "resolved_episode_id"):
        return state
    ids, alert_keys, episodes = zip(*wanted, strict=True)
    await cur.execute(
        """select h.rule_id,h.alert_key,h.episode,max(h.alert_id) as alert_id
           from unnest(%s::text[],%s::text[],%s::text[]) as w(rule_id,alert_key,episode)
           join resolved_alert_episodes h using(rule_id,alert_key,episode)
           group by h.rule_id,h.alert_key,h.episode""",
        (list(ids), list(alert_keys), list(episodes)),
    )
    for row in await cur.fetchall():
        state.resolved[row["rule_id"], row["alert_key"], row["episode"]] = row["alert_id"]
    if current:
        for (rid, key), row in current.items():
            state.remember_episode(rid, key, row["latest_episode"], row["id"])
        # An open incident may close in this batch, before the resolved-history view can see it.
        await cur.execute(
            """select distinct d.alert_id,coalesce(d.reason->>'episode',e.fields->>'$episode') as episode
               from decisions d join events e on e.id=d.event_id where d.alert_id=any(%s::bigint[])
               and d.reason->>'transition' in ('created','occurrence','severity_increased','cooldown_elapsed')
               and coalesce(d.reason->>'episode',e.fields->>'$episode')=any(%s::text[])""",
            ([row["id"] for row in current.values()], list(set(episodes))),
        )
        for row in await cur.fetchall():
            state.firing_episodes[row["alert_id"]].add(row["episode"])
    # Pre-episode writers kept the template inputs even when neither the decision
    # nor the canonical aliases carried the rendered identity. Inspect only the
    # candidate keys and schemas for which that older evidence could matter.
    missing = {entry for entry in wanted if entry not in state.resolved}
    templated = [sid for sid, schema in ruleset.schemas.items() if schema.get("episode_key")]
    if missing and templated:
        pairs = sorted({(r, k) for r, k, _ in missing})
        await cur.execute(
            """select a.rule_id,a.alert_key,a.id as alert_id,a.status,e.schema_id,e.fields
               from unnest(%s::text[],%s::text[]) as w(rule_id,alert_key)
               join alerts a using(rule_id,alert_key) join decisions d on d.alert_id=a.id
               join events e on e.id=d.event_id
               where d.reason->>'transition' in ('created','occurrence','severity_increased','cooldown_elapsed')
                 and d.reason->>'episode' is null and e.fields->>'$episode' is null
                 and e.schema_id=any(%s::text[])""",
            ([r for r, _ in pairs], [k for _, k in pairs], templated),
        )
        for row in await cur.fetchall():
            episode = episode_for(ruleset.schemas[row["schema_id"]], row["fields"])
            identity = row["rule_id"], row["alert_key"], episode
            if identity not in missing:
                continue
            if row["status"] == "resolved":
                state.resolved[identity] = max(row["alert_id"], state.resolved.get(identity, 0))
            else:
                state.firing_episodes[row["alert_id"]].add(episode)
    return state


async def lock_open(cur, keys):
    state = {}
    for rid, key in sorted(keys):
        await cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (f"{rid}:{key}",))
        await cur.execute(
            "select * from alerts where rule_id=%s and alert_key=%s and status<>'resolved' for update",
            (rid, key),
        )
        row = await cur.fetchone()
        if row:
            state[rid, key] = row
    return state


async def persist(cur, row):
    columns = [*COLUMNS, "spike_state"] if "spike_state" in row else COLUMNS
    values = [
        Jsonb(row[k]) if k in ("fields", "group_values", "score_breakdown", "spike_state") else row.get(k)
        for k in columns
    ]
    cols = list(map(sql.Identifier, columns))
    updates = [sql.SQL("{}=excluded.{}").format(c, c) for c in cols[1:]]
    query = sql.SQL(
        "insert into alerts ({}) values ({}) on conflict (id) do update set {},updated_at=now()"
    )
    await cur.execute(
        query.format(
            sql.SQL(",").join(cols),
            sql.SQL(",").join(sql.Placeholder() for _ in cols),
            sql.SQL(",").join(updates),
        ),
        values,
    )


async def process_batch(db, settings: Settings | None = None, lane="normal", batch=200, stop=None):
    if batch == 0 or (stop is not None and stop.is_set()):
        return 0
    if batch < 0:
        raise ValueError("batch must be nonnegative")
    if settings is None:
        settings = await get_settings(db)
    async with (
        db.connection() as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        rows = await claim_sources(cur, lane, batch, stop)
        if rows:
            await isolate(conn, cur, settings, lane, rows)
        return len(rows)


async def claim_sources(cur, lane, batch, stop):
    # Walk distinct source keys in the pending index instead of ranking the entire backlog.
    await cur.execute(
        """with recursive active(source_id) as (
               (select source_id from events where lane=%s and status='pending'
                order by source_id limit 1)
               union all
               select following.source_id from active
               cross join lateral (
                   select e.source_id from events e where e.lane=%s and e.status='pending'
                     and e.source_id > active.source_id order by e.source_id limit 1
               ) following
           ) select source_id from active order by source_id""", (lane, lane),
    )
    sources = [r["source_id"] for r in await cur.fetchall()]
    if not sources:
        return []
    if len(sources) > 1:
        sequence = "event_critical_claim_turn" if lane == "critical" else "event_normal_claim_turn"
        await cur.execute("select nextval(%s::regclass) - 1 as turn", (sequence,))
        start = (await cur.fetchone())["turn"] % len(sources)
        sources = sources[start:] + sources[:start]
    sources = sources[:batch]
    quota, remainder = divmod(batch, len(sources))
    if stop is not None and stop.is_set():
        return []
    await cur.execute(
        """select picked.* from unnest(%s::text[]) with ordinality as s(source, pos)
           cross join lateral (
               select e.* from events e where e.source_id=s.source and e.lane=%s
                 and e.status='pending' order by e.id
               limit (%s::integer + (s.pos <= %s)::integer) for update skip locked
           ) picked""", (sources, lane, quota, remainder),
    )
    rows = await cur.fetchall()
    if len(rows) < batch and (stop is None or not stop.is_set()):
        # Empty queues or locks held by another worker should not waste the remaining slots.
        await cur.execute(
            """select * from events where lane=%s and status='pending' and id<>all(%s::bigint[])
               order by id limit %s for update skip locked""",
            (lane, [row["id"] for row in rows], batch - len(rows)),
        )
        rows.extend(await cur.fetchall())
    return sorted(rows, key=lambda row: row["id"])


async def isolate(conn, cur, settings, lane, rows):
    try:
        async with conn.transaction():
            events = []
            for row in rows:
                try:
                    event = CanonicalEvent.model_validate(row)
                    clock_for_event(event.trace)
                except (ValueError, TypeError) as exc:
                    raise BadEvent("Invalid canonical event or event clock") from exc
                if demo_mode() and event.trace.get("demo_fault") == "raise":
                    raise BadEvent("Injected demo fault")
                events.append(event)
            await apply_events(cur, settings, lane, events)
        return
    except (BadEvent, RuleFault) as exc:
        failure = exc
    if len(rows) > 1:
        mid = len(rows) // 2
        await isolate(conn, cur, settings, lane, rows[:mid])
        await isolate(conn, cur, settings, lane, rows[mid:])
        return
    row = rows[0]
    if isinstance(failure, RuleFault):
        rule = failure.rule
        team = settings.directory.get("fallback_team")
        detail = {"error": str(failure), "team_id": team}
        await cur.execute(
            "insert into decisions(event_id,rule_id,rule_version,outcome,reason) values(%s,%s,%s,'RULE_FAULT',%s)",
            (row["id"], rule["id"], rule.get("version", 1), Jsonb(detail)),
        )
        await cur.execute(
            "insert into audit_log(actor,action,target_kind,target_id,detail) values('system','rule.fault','team',%s,%s)",
            (team, Jsonb(dict(detail, event_id=row["id"], rule_id=rule["id"]))),
        )
        remaining = copy(settings.ruleset)
        remaining.rules = [(r, pred) for r, pred in remaining.rules if r["id"] != rule["id"]]
        await isolate(conn, cur, replace(settings, ruleset=remaining), lane, rows)
        return
    trace = row["trace"] if isinstance(row["trace"], dict) else {}
    prior = trace.get("attempts", 0)
    attempts = min(3, max(0, prior) + 1) if type(prior) is int else 1
    reason = {"attempts": attempts, "processing_error": str(failure)}
    await cur.execute(
        "update events set status=%s,trace=%s where id=%s",
        ("quarantined" if attempts == 3 else "pending", Jsonb({**trace, **reason}), row["id"]),
    )
    if attempts == 3:
        await cur.execute(
            "insert into decisions(event_id,outcome,reason) values(%s,'QUARANTINED',%s)",
            (row["id"], Jsonb(reason)),
        )


async def apply_events(cur, settings, lane, events):
    if not events:
        return 0
    keys = set()
    episodes = set()
    prepared = []
    for event in events:
        eligible = eligible_ruleset(event, settings.ruleset)
        watches_heartbeats = any("expect" in rule for rule, _ in eligible.rules)
        ruleset = eligible
        if watches_heartbeats:
            # A heartbeat advances its watch without creating a live failure.
            ruleset = copy(eligible)
            ruleset.rules = [(rule, pred) for rule, pred in eligible.rules if "expect" not in rule]
        clock = FixedClock(clock_for_event(event.trace).now())
        prepared.append((event, ruleset, eligible if watches_heartbeats else None, clock))
        if not ruleset.rules:
            continue
        schema = ruleset.schemas.get(event.schema_id, {})
        resolve = ruleset.resolvers.get(event.schema_id)
        resolving = resolve and resolve(event.fields)[0]
        for rule, predicate in ruleset.rules:
            with evaluating(rule):
                if resolving or predicate(event.fields)[0]:
                    key = grouping(rule, schema, event.fields)[0]
                    keys.add((rule["id"], key))
                    episode = episode_for(schema, event.fields)
                    if episode is not None:
                        episodes.add((rule["id"], key, episode))
    matched_ids = {rid for rid, _ in keys}
    await lock_rules(cur, (r for r, _ in settings.ruleset.rules if r["id"] in matched_ids))
    current = await lock_open(cur, keys)
    state = await load_batch_state(
        cur, current, keys, episodes, settings.ruleset, settings, min(p[3].now() for p in prepared)
    )
    dirty, new_ids, decisions, intents = {}, set(), [], []
    samples = defaultdict(list)
    for event, ruleset, heartbeat_rules, clock in prepared:
        at = clock.now()
        result = evaluate(event, ruleset, state, clock)
        if heartbeat_rules is not None and await record_heartbeats(
            cur, replace(settings, ruleset=heartbeat_rules), event, clock
        ):
            result.decisions = [d for d in result.decisions if d["rule_id"] is not None]
        if getattr(ruleset, "shadow_rules", ()):
            from alerting.rules.shadow import evaluate_shadow

            await evaluate_shadow(cur, event, ruleset, clock, silences_for_rule=state.silences_for_rule)
        for op in result.alert_ops:
            rule = op.rule
            with evaluating(rule):
                identity = (rule["id"], op.key)
                row = state.get(identity)
                if not row:
                    await cur.execute("select nextval(pg_get_serial_sequence('alerts','id')) as id")
                    id_ = (await cur.fetchone())["id"]
                    row = dict(
                        id=id_,
                        rule_id=rule["id"],
                        alert_key=op.key,
                        rule_version=rule.get("version", 1),
                        status="open",
                        team_id=team_for(rule.get("owner", ""), settings.directory),
                        severity=event.severity.value,
                        lane=lane,
                        occurrence_count=0,
                        count_since_notify=0,
                        first_seen_at=at,
                        last_notified_at=None,
                    )
                    state[identity] = row
                    new_ids.add(id_)
                id_ = row["id"]
                dirty[id_] = row
                decision = next(d for d in result.decisions if d["rule_id"] == rule["id"])
                decision["alert_id"] = id_
                if decision["reason"].get("notification_cause") == "first_eligible_after_silence":
                    decision["reason"]["eligible_at"] = at.isoformat()
                if op.transition in ("resolved", "resolve_pending"):
                    stable_for = rule.get("group", {}).get("stable_for")
                    if stable_for and duration(stable_for).total_seconds() > 0:
                        start_recovery(row, event.id, at, duration(stable_for))
                        decision.update(outcome="RESOLVE_PENDING")
                        decision["reason"]["transition"] = "resolve_pending"
                        continue
                    cancel_recovery(row)
                    row.update(
                        status="resolved",
                        resolved_by="system",
                        resolved_at=at,
                        resolve_reason="resolve_when",
                    )
                    state.close_episodes(*identity, id_)
                    del state[identity]
                    continue
                if row.get("resolve_pending_at") is not None:
                    cancel_recovery(row)
                episode = episode_for(ruleset.schemas.get(event.schema_id, {}), event.fields)
                if episode is not None:
                    state.remember_episode(*identity, episode, id_)
                silenced = decision["outcome"] == "SILENCED"
                row.update(
                    latest_episode=episode if episode is not None else row.get("latest_episode"),
                    rule_version=rule.get("version", 1),
                    title=op.title,
                    body=op.body,
                    color=rule.get("present", {}).get("color"),
                    fields=op.fields,
                    group_values=op.group_values,
                    representative_event_id=event.id,
                    schema_id=event.schema_id,
                    sensitive_paths=ruleset.schemas.get(event.schema_id, {}).get("sensitive", []),
                    last_seen_at=at,
                    occurrence_count=row["occurrence_count"] + 1,
                    count_since_notify=row["count_since_notify"] + int(not silenced),
                )
                severity = event.severity
                if HAS_SPIKE_STATE:
                    severity = op.effective_severity or severity
                    if op.spike_state is not None or "spike_state" in row:
                        row["spike_state"] = op.spike_state or {}
                if SEVERITY_ORDER.get(severity, -1) > SEVERITY_ORDER.get(row["severity"], -1):
                    row["severity"] = severity.value
                score_inputs = (
                    {"fields": event.fields, "schema": ruleset.schemas.get(event.schema_id, {})}
                    if SCORE_ACCEPTS_SCHEMA else {}
                )
                row["priority"], row["score"], row["score_breakdown"] = score(
                    row["severity"],
                    event.environment or event.fields.get("$environment"),
                    row["occurrence_count"],
                    rule,
                    **score_inputs,
                )
                fields = {**event.fields, "gen_sent_at": event.trace.get("gen_sent_at")}
                # Project eligibility so the next event sees the correct cooldown in this batch.
                route_transition = getattr(op, "routing_transition", None) or op.transition
                routing = plan(
                    row,
                    route_transition,
                    rule,
                    fields,
                    settings.directory,
                    settings.runbooks,
                    {},
                    clock,
                )
                row["runbook_id"] = routing.runbook_id
                samples[id_].append((id_, event.id, at))
                if silenced:
                    continue
                if op.notify and routing.intents:
                    row.update(last_notified_at=at, count_since_notify=0)
                    intents.append((dict(row), route_transition, rule, fields, clock, decision))
                elif op.notify and routing.unrouted_reason:
                    decision.update(outcome="UNROUTED")
                    decision["reason"]["routing"] = routing.unrouted_reason
                group = rule.get("group", {})
                remind_open = (
                    group.get("mode", "immediate") == "immediate" and row["last_notified_at"]
                )
                if (row["count_since_notify"] or remind_open) and (
                    not row.get("next_notify_at") or op.notify
                ):
                    interval = duration(
                        group.get("every", "5m")
                        if group.get("mode") == "digest"
                        else group.get("cooldown", "10m")
                    )
                    anchor = row["last_notified_at"] or row["first_seen_at"]
                    row["next_notify_at"] = SYSTEM.now() + max(
                        anchor + interval - at, duration("0s")
                    )
                elif not row["count_since_notify"] and not remind_open:
                    row["next_notify_at"] = None
        decisions.extend((event.id, d) for d in result.decisions)
    for row in dirty.values():
        await persist(cur, row)
    planned = []
    for snapshot, transition, rule, fields, clock, decision in intents:
        routing = await delivery_plan(cur, snapshot, transition, rule, fields, settings, clock)
        planned.append((snapshot, rule, routing.intents))
        decision["reason"]["notifications"] = [i.dedup_key for i in routing.intents]
        summaries = [
            i.rendered.meta["throttle_bucket"]
            for i in routing.intents
            if i.kind == "throttle_summary"
        ]
        if summaries:
            decision["reason"]["throttle_buckets"] = summaries
        if routing.intents and len(summaries) == len(routing.intents):
            decision["outcome"] = "SUPPRESSED_THROTTLE"
    await enqueue_event_plans(cur, planned)
    await cur.executemany(
        "insert into decisions(event_id,rule_id,rule_version,outcome,reason,alert_id) "
        "values (%s,%s,%s,%s,%s,%s)",
        [
            (
                eid,
                d["rule_id"],
                d["rule_version"],
                d["outcome"],
                Jsonb(d["reason"]),
                d.get("alert_id"),
            )
            for eid, d in decisions
        ],
    )
    for aid, rows in samples.items():
        await cur.executemany(
            "insert into alert_occurrences(alert_id,event_id,seen_at) values (%s,%s,%s)",
            rows[-50:],
        )
        await cur.execute(
            """delete from alert_occurrences where alert_id=%s and event_id in
            (select event_id from alert_occurrences where alert_id=%s order by event_id desc offset 50)""",
            (aid, aid),
        )
    await cur.execute(
        """update events set status='processed',processed_at=now(),
        trace=trace || jsonb_build_object('processed_at',clock_timestamp()) where id=any(%s)""",
        ([e.id for e in events],),
    )
    for row in dirty.values():
        await alert_changed(cur, row, row["id"] in new_ids)
    return len(events)


async def main(lane, batch):
    from alerting.delivery.shutdown import termination_event, wait_or_stop

    with termination_event() as stop:
        await open_pool()
        try:
            while not stop.is_set():
                try:
                    claimed = await process_batch(pool(), lane=lane, batch=batch, stop=stop)
                except psycopg.Error as exc:
                    if not transient(exc):
                        raise
                    logging.getLogger(__name__).warning(
                        "Transient processing failure; retrying", exc_info=True
                    )
                    await wait_or_stop(stop, 0.5)
                    continue
                if not claimed:
                    await wait_or_stop(stop, 0.15)
        finally:
            await close_settings(pool())
            await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=["normal", "critical"], default="normal")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()
    if args.batch <= 0:
        parser.error("--batch must be positive")
    asyncio.run(main(args.lane, args.batch))
