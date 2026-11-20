"""One lane per process. Transaction-scoped locks serialize only overlapping rule/key pairs."""
import argparse
import asyncio
from collections import defaultdict

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM, clock_for_event
from alerting.contracts import CanonicalEvent, SEVERITY_ORDER
from alerting.db import pool, open_pool, close_pool
from alerting.routing import plan
from alerting.routing.directory import team_for
from alerting.rules.engine import evaluate, grouping
from alerting.rules.storage import Settings, alert_changed, enqueue

COLUMNS = ("id rule_id rule_version alert_key status severity priority lane team_id title body color fields group_values "
           "runbook_id occurrence_count count_since_notify first_seen_at last_seen_at last_notified_at "
           "representative_event_id resolved_by resolved_at resolve_reason").split()


async def lock_open(cur, keys):
    state = {}
    for rid, key in sorted(keys):
        await cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (f"{rid}:{key}",))
        await cur.execute("select * from alerts where rule_id=%s and alert_key=%s and status<>'resolved' for update",
                          (rid, key))
        row = await cur.fetchone()
        if row:
            state[rid, key] = row
    return state


async def persist(cur, row):
    values = [Jsonb(row[k]) if k in ("fields", "group_values", "score_breakdown") else row.get(k) for k in COLUMNS]
    cols = list(map(sql.Identifier, COLUMNS))
    updates = [sql.SQL("{}=excluded.{}").format(c, c) for c in cols[1:]]
    query = sql.SQL("insert into alerts ({}) values ({}) on conflict (id) do update set {},updated_at=now()")
    await cur.execute(query.format(sql.SQL(",").join(cols), sql.SQL(",").join(sql.Placeholder() for _ in cols),
                                   sql.SQL(",").join(updates)), values)


async def process_batch(db, settings, lane="normal", batch=200):
    async with db.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("""select * from events where status='pending' and lane=%s
                             order by id limit %s for update skip locked""", (lane, batch))
        events = [CanonicalEvent.model_validate(r) for r in await cur.fetchall()]
        if not events:
            return 0
        keys = set()
        ruleset = settings.ruleset
        for event in events:
            schema = ruleset.schemas.get(event.schema_id, {})
            resolve = ruleset.resolvers.get(event.schema_id)
            resolving = resolve and resolve(event.fields)[0]
            for rule, predicate in ruleset.rules:
                if resolving or predicate(event.fields)[0]:
                    keys.add((rule["id"], grouping(rule, schema, event.fields)[0]))
        state = await lock_open(cur, keys)
        dirty, new_ids, decisions, intents = {}, set(), [], []
        samples = defaultdict(list)
        for event in events:
            clock = clock_for_event(event.trace)
            at = clock.now()
            result = evaluate(event, ruleset, state, clock)
            for op in result.alert_ops:
                rule = op.rule
                identity = (rule["id"], op.key)
                row = state.get(identity)
                if not row:
                    await cur.execute("select nextval(pg_get_serial_sequence('alerts','id')) as id")
                    id_ = (await cur.fetchone())["id"]
                    row = dict(id=id_, rule_id=rule["id"], alert_key=op.key, rule_version=rule.get("version", 1),
                               status="open", team_id=team_for(rule.get("owner", ""), settings.directory),
                               severity=event.severity.value, lane=lane, occurrence_count=0, count_since_notify=0,
                               first_seen_at=at, last_notified_at=None)
                    state[identity] = row
                    new_ids.add(id_)
                id_ = row["id"]
                dirty[id_] = row
                decision = next(d for d in result.decisions if d["rule_id"] == rule["id"])
                decision["alert_id"] = id_
                if op.transition == "resolved":
                    row.update(status="resolved", resolved_by="system", resolved_at=at, resolve_reason="resolve_when")
                    del state[identity]
                    continue
                row.update(title=op.title, body=op.body, color=rule.get("present", {}).get("color"),
                           fields=op.fields, group_values=op.group_values, representative_event_id=event.id,
                           last_seen_at=at, occurrence_count=row["occurrence_count"]+1,
                           count_since_notify=row["count_since_notify"]+1)
                if SEVERITY_ORDER.get(event.severity, -1) > SEVERITY_ORDER.get(row["severity"], -1):
                    row["severity"] = event.severity.value
                fields = {**event.fields, "gen_sent_at": event.trace.get("gen_sent_at")}
                routing = plan(row, op.transition, rule, fields, settings.directory, settings.runbooks, {}, clock)
                row["runbook_id"] = routing.runbook_id
                samples[id_].append((id_, event.id, at))
                if op.notify and routing.intents:
                    row.update(last_notified_at=at, count_since_notify=0)
                    intents.append((routing.intents, row["team_id"]))
                    decision["reason"]["notifications"] = [i.dedup_key for i in routing.intents]
                elif op.notify and routing.unrouted_reason:
                    decision.update(outcome="UNROUTED")
                    decision["reason"]["routing"] = routing.unrouted_reason
            decisions.extend((event.id, d["rule_id"], d["rule_version"], d["outcome"], Jsonb(d["reason"]),
                              d.get("alert_id")) for d in result.decisions)
        for row in dirty.values():
            await persist(cur, row)
        for messages, team in intents:
            await enqueue(cur, messages, team)
        await cur.executemany("insert into decisions(event_id,rule_id,rule_version,outcome,reason,alert_id) "
                              "values (%s,%s,%s,%s,%s,%s)", decisions)
        for aid, rows in samples.items():
            await cur.executemany("insert into alert_occurrences(alert_id,event_id,seen_at) values (%s,%s,%s)",
                                  rows[-50:])
            await cur.execute("""delete from alert_occurrences where alert_id=%s and event_id in
                (select event_id from alert_occurrences where alert_id=%s order by event_id desc offset 50)""", (aid, aid))
        await cur.execute("""update events set status='processed',processed_at=now(),
            trace=trace || jsonb_build_object('processed_at',clock_timestamp()) where id=any(%s)""",
            ([e.id for e in events],))
        for row in dirty.values():
            await alert_changed(cur, row, row["id"] in new_ids)
        return len(events)


async def main(lane, batch):
    await open_pool()
    settings = Settings.load()
    try:
        while True:
            if not await process_batch(pool(), settings, lane, batch):
                await asyncio.sleep(0.15)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=["normal", "critical"], default="normal")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()
    if args.batch <= 0:
        parser.error("--batch must be positive")
    asyncio.run(main(args.lane, args.batch))
