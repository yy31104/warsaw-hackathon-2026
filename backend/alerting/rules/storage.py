"""Transaction-scoped database operations shared by workers and HTTP routes."""

import asyncio
import hashlib
import inspect
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from time import monotonic
from weakref import WeakKeyDictionary

import psycopg
import yaml

from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.rules.engine import Ruleset
from alerting.schemas.registry import schemas as active_schemas


@dataclass
class Settings:
    ruleset: Ruleset
    directory: dict
    runbooks: list

    @classmethod
    def load(cls):
        return cls(Ruleset(files.rules(), files.schemas()), files.directory(), files.runbooks())


log = logging.getLogger(__name__)
_providers = WeakKeyDictionary()


def file_signature():
    root = Path(files.CONFIG_DIR)
    return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()))


def config_loader():
    try:
        return import_module("alerting.config.loader")
    except ModuleNotFoundError as exc:
        if exc.name != "alerting.config.loader":
            raise
        return None


async def registry_settings(db):
    loader = config_loader()
    if loader is None:
        return None
    values = []
    for name in ("rules", "directory", "runbooks"):
        result = getattr(loader, name)()
        values.append(await result if inspect.isawaitable(result) else result)
    rules, directory, books = values
    if not (rules or books or (directory or {}).get("teams") or (directory or {}).get("users")):
        return None
    async with db.connection() as conn:
        # Reviewed schemas and their detection order come from the ingest registry.
        # A config loader may be present before it implements schema review itself.
        schemas = await active_schemas(conn)
        if any("version" not in rule for rule in rules):
            rows = await (
                await conn.execute(
                    "select id,version from config_objects where kind='rule' and active"
                )
            ).fetchall()
            versions = dict(rows)
            rules = [dict(r, version=r.get("version") or versions[r["id"]]) for r in rules]
    return Settings(Ruleset(rules, schemas), directory, books)


async def file_settings(db):
    rules = files.rules()
    directory, books = files.directory(), files.runbooks()
    # Coordinate versions across processes and restarts, even when a file has no version field.
    async with db.connection() as conn:
        schemas = await active_schemas(conn)
        for rule in sorted(rules, key=lambda r: r["id"]):
            content = json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()
            row = await (
                await conn.execute(
                    """
                insert into backend_file_rule_revisions as old(rule_id,content_hash,version)
                values(%s,%s,%s) on conflict(rule_id) do update set content_hash=excluded.content_hash,
                    version=case when old.content_hash=excluded.content_hash then old.version
                                 else greatest(old.version+1,excluded.version) end returning version
                """,
                    (rule["id"], hashlib.sha256(content).hexdigest(), rule.get("version", 1)),
                )
            ).fetchone()
            rule["version"] = row[0]
        return Settings(Ruleset(rules, schemas), directory, books)


class SettingsProvider:
    def __init__(self, conninfo):
        self.conninfo = conninfo
        self.lock = asyncio.Lock()
        self.current = None
        self.signature = None
        self.generation = 0
        self.loaded_generation = -1
        self.next_check = 0.0
        self.listener = asyncio.create_task(self.listen())

    async def listen(self):
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.conninfo, autocommit=True, connect_timeout=3
                ) as conn:
                    await conn.execute("listen config")
                    # A fresh subscription also covers notifications missed while disconnected.
                    self.generation += 1
                    async for _ in conn.notifies():
                        self.generation += 1
            except psycopg.Error:
                log.warning("Config listener disconnected; reconnecting", exc_info=True)
                await asyncio.sleep(1)

    async def get(self, db):
        async with self.lock:
            generation = self.generation
            now = monotonic()
            poll = now >= self.next_check
            if self.current is not None and not poll and generation == self.loaded_generation:
                return self.current
            try:
                signature = self.signature
                if poll:
                    self.next_check = now + 2
                    signature = file_signature()
                if (
                    self.current is None
                    or signature != self.signature
                    or generation != self.loaded_generation
                ):
                    snapshot = await registry_settings(db) or await file_settings(db)
                    self.current, self.signature = snapshot, signature
                    self.loaded_generation = generation
            except (OSError, ValueError, KeyError, yaml.YAMLError, psycopg.Error):
                if self.current is None:
                    raise
                log.exception("Keeping last valid alert configuration")
            return self.current


async def prepare_settings(db):
    loader = config_loader()
    if hasattr(loader, "sync_files"):
        await loader.sync_files(db)


async def get_settings(db, *, conn=None):
    loader = config_loader()
    if hasattr(loader, "read_settings"):
        if conn is not None:
            return await loader.read_settings(db, conn)
        async with db.connection() as scoped:
            return await loader.read_settings(db, scoped)
    provider = _providers.get(db)
    if provider is None:
        _providers[db] = provider = SettingsProvider(db.conninfo)
    return await provider.get(db)


async def close_settings(db):
    provider = _providers.pop(db, None)
    if provider:
        provider.listener.cancel()
        with suppress(asyncio.CancelledError):
            await provider.listener


EVENT_CHANNELS = {
    "alert.created": "alerts",
    "alert.updated": "alerts",
    "notification.updated": "notifications",
    "action.updated": "actions",
}


async def emit(cur, channel, topic, *, id, team_id, **changes):
    if EVENT_CHANNELS.get(topic) != channel or channel is None:
        raise ValueError(f"Unknown backend event or channel: {topic} on {channel}")
    data = {**changes, "id": id, "team_id": team_id, "ts": SYSTEM.now().isoformat()}
    await cur.execute(
        "select pg_notify(%s,%s)", (channel, json.dumps({"type": topic, "data": data}, default=str))
    )


async def enqueue(cur, intents, team_id):
    for i in intents:
        ui = i.channel == "ui"
        if i.kind == "escalation" and "masked" not in i.rendered.meta:
            await apply_legacy_mask(cur, i)
        if i.kind == "throttle_summary":
            from datetime import timedelta

            message = i.rendered.model_dump(mode="json")
            await cur.execute(
                """insert into notifications(alert_id,kind,recipient,channel,contact,rendered,priority,dedup_key,
                state,attempts,delivered_at,next_attempt_at,throttle_bucket)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(throttle_bucket) do update
                set rendered=notifications.rendered || jsonb_build_object('title',excluded.rendered->'title',
                'body',excluded.rendered->'body','short',excluded.rendered->'short','meta',
                notifications.rendered->'meta' || jsonb_build_object('suppressed_count',excluded.rendered->'meta'->'suppressed_count'))
                returning id,state,attempts""",
                (
                    i.alert_id,
                    i.kind,
                    i.recipient,
                    i.channel.value,
                    i.contact,
                    Jsonb(message),
                    i.priority,
                    i.dedup_key,
                    "delivered" if ui else "pending",
                    int(ui),
                    SYSTEM.now() if ui else None,
                    SYSTEM.now() + timedelta(seconds=message["meta"]["due_in"]),
                    message["meta"]["throttle_bucket"],
                ),
            )
            row = await cur.fetchone()
            await emit(
                cur,
                "notifications",
                "notification.updated",
                id=row["id"],
                team_id=team_id,
                state=row["state"],
                attempts=row["attempts"],
            )
            continue
        await insert_notification(cur, i, team_id)


async def insert_notification(cur, intent, team_id, merged_into=None):
    ui = intent.channel == "ui"
    state = "delivered" if ui else "pending"
    await cur.execute(
        """insert into notifications(alert_id,kind,recipient,channel,contact,rendered,
        priority,dedup_key,state,attempts,delivered_at,merged_into)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (dedup_key) where dedup_key is not null do nothing returning id""",
        (intent.alert_id, intent.kind, intent.recipient, intent.channel.value, intent.contact,
         Jsonb(intent.rendered.model_dump(mode="json")), intent.priority, intent.dedup_key,
         state, int(ui), SYSTEM.now() if ui else None, merged_into),
    )
    row = await cur.fetchone()
    if row:
        await emit(cur, "notifications", "notification.updated", id=row["id"], alert_id=intent.alert_id,
                   team_id=team_id, state=state, attempts=int(ui))
        return row["id"]


async def apply_legacy_mask(cur, intent):
    """Manual escalation of an incident created before its schema policy was snapshotted."""
    from alerting.routing.plan import render_message

    await cur.execute(
        """select a.title,a.body,a.fields,a.sensitive_paths,e.fields as event_fields,e.schema_id,
                  (select spec from config_objects c where c.kind='schema' and c.id=e.schema_id
                   and (c.version=e.schema_version or c.active)
                   order by (c.version=e.schema_version) desc nulls last,c.version desc limit 1) as schema
           from alerts a left join events e on e.id=a.representative_event_id where a.id=%s""",
        (intent.alert_id,),
    )
    row = await cur.fetchone()
    paths = row["sensitive_paths"]
    if paths is None:
        schema = row["schema"] or next((s for s in files.schemas() if s["id"] == row["schema_id"]), None)
        paths = schema.get("sensitive", []) if schema is not None else None
    msg = intent.rendered
    # Without a trusted schema, legacy external notifications can still give an incident link.
    if paths is None:
        intent.rendered = render_message(intent.channel, f"Alert {intent.alert_id}", "", {}, None,
                                         msg.link, msg.color, {**msg.meta, "masked": True}, {}, None)
        return
    # Complete alert text lets us apply masking before calculating the SMS or phone length.
    excerpt = msg.runbook_excerpt
    if paths and excerpt and len(excerpt) == 512:
        excerpt = None  # An old excerpt may end midway through a private value.
    intent.rendered = render_message(intent.channel, row["title"] or msg.title, row["body"] or "",
                                     row["fields"], excerpt, msg.link, msg.color, msg.meta,
                                     row["event_fields"] or {}, paths)


async def enqueue_event_plans(cur, planned):
    from alerting.routing.merge import combined, groups

    for members in groups(planned):
        if len(members) == 1:
            only = members[0]
            await enqueue(cur, [only.intent], only.alert["team_id"])
            continue
        # A critical incident gives the shared delivery its priority immediately.
        members.sort(key=lambda m: (m.intent.priority, m.alert["rule_id"], m.intent.alert_id))
        physical = None
        for member in members:
            alert, intent = member.alert, member.intent
            rendered = combined(members) if physical is None else intent.rendered
            merged = intent.model_copy(update={"kind": "merged", "rendered": rendered})
            notification = await insert_notification(cur, merged, alert["team_id"], merged_into=physical)
            if notification is None:
                # The original batch and every logical link are committed atomically.
                break
            if physical is None:
                physical = notification
            detail = {"notification_id": str(notification), "physical_notification_id": str(physical),
                      "link": intent.rendered.link, "notifications": [intent.dedup_key]}
            await cur.execute(
                """insert into decisions(event_id,rule_id,rule_version,outcome,reason,alert_id)
                   values (%s,%s,%s,'MERGED',%s,%s)""",
                (alert["representative_event_id"], alert["rule_id"], alert["rule_version"],
                 Jsonb(detail), intent.alert_id),
            )
            await audit(cur, "system", "notification.merged", "alert", intent.alert_id, detail)


async def alert_changed(cur, row, created=False):
    keys = ("id", "team_id", "occurrence_count", "status", "severity", "priority", "score")
    await emit(
        cur,
        "alerts",
        "alert.created" if created else "alert.updated",
        **({"masked": bool(row.get("sensitive_paths"))} if created else {}),
        **{k: row.get(k) for k in keys},
    )


async def audit(cur, actor, action, target, target_id, detail=None):
    await cur.execute(
        """insert into audit_log(actor,action,target_kind,target_id,detail)
                         values (%s,%s,%s,%s,%s)""",
        (actor, action, target, str(target_id), Jsonb(detail or {})),
    )


async def get_db():
    from alerting.db import pool

    return pool()


async def delivery_plan(cur, alert, transition, rule, fields, settings, clock):
    from alerting.routing import plan
    from alerting.routing.throttle import apply, reserve

    schema_id = alert.get("schema_id")
    if "schema_id" not in alert and alert.get("representative_event_id"):
        await cur.execute("select schema_id from events where id=%s", (alert["representative_event_id"],))
        event = await cur.fetchone()
        schema_id = event["schema_id"] if event else None
    paths = set(alert.get("sensitive_paths") or ())
    paths.update(settings.ruleset.schemas.get(schema_id, {}).get("sensitive", ()))
    initial = plan(
        alert,
        transition,
        {k: v for k, v in rule.items() if k != "throttle"},
        fields,
        settings.directory,
        settings.runbooks,
        {},
        clock,
        sensitive_paths=paths,
    )
    if rule.get("throttle") and initial.intents:
        counts = await reserve(cur, rule, initial.intents, clock)
        for intent in initial.intents:
            apply(intent, rule, counts, clock)
    return initial
