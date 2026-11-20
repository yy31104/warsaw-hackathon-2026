"""Transaction-scoped database operations shared by workers and HTTP routes."""
import json
from dataclasses import dataclass
from psycopg.types.json import Jsonb
from alerting.clock import SYSTEM
from alerting.config import files
from alerting.rules.engine import Ruleset


@dataclass
class Settings:
    ruleset: Ruleset
    directory: dict
    runbooks: list

    @classmethod
    def load(cls):
        return cls(Ruleset(files.rules(), files.schemas()), files.directory(), files.runbooks())


async def emit(cur, channel, topic, **data):
    data.setdefault("ts", SYSTEM.now().isoformat())
    await cur.execute("select pg_notify(%s,%s)", (channel, json.dumps({"type": topic, "data": data}, default=str)))


async def enqueue(cur, intents, team_id):
    for i in intents:
        ui = i.channel == "ui"
        await cur.execute("""insert into notifications(alert_id,kind,recipient,channel,contact,rendered,
            priority,dedup_key,state,attempts,delivered_at)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (dedup_key) where dedup_key is not null do nothing returning id""",
            (i.alert_id, i.kind, i.recipient, i.channel.value, i.contact, Jsonb(i.rendered.model_dump(mode="json")),
             i.priority, i.dedup_key, "delivered" if ui else "pending", int(ui), SYSTEM.now() if ui else None))
        row = await cur.fetchone()
        if row:
            await emit(cur, "notifications", "notification.updated", id=row["id"], alert_id=i.alert_id,
                       team_id=team_id, state="delivered" if ui else "pending", attempts=int(ui))


async def alert_changed(cur, row, created=False):
    keys = ("id", "team_id", "occurrence_count", "status", "severity", "priority", "score")
    await emit(cur, "alerts", "alert.created" if created else "alert.updated",
               **{k: row.get(k) for k in keys})


async def audit(cur, actor, action, target, target_id, detail=None):
    await cur.execute("""insert into audit_log(actor,action,target_kind,target_id,detail)
                         values (%s,%s,%s,%s,%s)""", (actor, action, target, str(target_id), Jsonb(detail or {})))


async def get_db():
    from alerting.db import pool
    return pool()

