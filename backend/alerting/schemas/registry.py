"""Versioned schema registry shared by HTTP, AMQP and file imports."""
import hashlib
import json
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb

from alerting.config import files
from alerting.schemas.issues import notify


class ReviewError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def lock_registry(conn, *, write=False):
    # A batch that classified against the old registry commits before the reviewer
    # scans pending rows. Other ingest batches can proceed concurrently.
    if write:
        await conn.execute("SELECT pg_advisory_xact_lock('schema_issues'::regclass::oid::int, 201)")
    else:
        await conn.execute("SELECT pg_advisory_xact_lock_shared('schema_issues'::regclass::oid::int, 201)")


async def schemas(conn, file_specs=None):
    base = files.schemas() if file_specs is None else file_specs
    ordered = [{**s, "version": s.get("version", 1), "origin": "file"} for s in base]
    cursor = await conn.execute("""
        SELECT active.spec, active.version, active.origin FROM config_objects active
        JOIN config_objects first ON (first.kind, first.id)=(active.kind, active.id) AND first.version=1
        WHERE active.kind='schema' AND active.active AND active.origin <> 'file'
        ORDER BY first.created_at, first.id
    """)
    file_ids = {s["id"] for s in base}
    for spec, version, origin in await cursor.fetchall():
        if spec["id"] not in file_ids:
            ordered.append({**spec, "version": version, "origin": origin})
    return ordered


@asynccontextmanager
async def ingest_registry(pool, file_specs):
    async with pool.connection() as conn, conn.transaction():
        await lock_registry(conn)
        yield conn, await schemas(conn, file_specs)


def with_draft(active, spec):
    candidate = list(active)
    for index, old in enumerate(active):
        if old["id"] != spec["id"]:
            continue
        if old["origin"] == "file":
            raise ReviewError(409, "This schema is managed in a configuration file")
        old_spec = {k: v for k, v in old.items() if k not in ("version", "origin")}
        changed = old_spec != spec
        version = old["version"] + int(changed)
        candidate[index] = {**spec, "version": version, "origin": "ui"}
        return candidate, version, changed
    candidate.append({**spec, "version": 1, "origin": "ui"})
    return candidate, 1, True


async def write_version(conn, spec, version, actor):
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    await conn.execute("UPDATE config_objects SET active=false WHERE kind='schema' AND id=%s AND active", (spec["id"],))
    await conn.execute("""
        INSERT INTO config_objects (kind, id, version, spec, content_hash, origin, created_by)
        VALUES ('schema',%s,%s,%s,%s,'ui',%s)
    """, (spec["id"], version, Jsonb(spec), digest, actor))
    await conn.execute("""
        INSERT INTO audit_log (actor, action, target_kind, target_id, detail)
        VALUES (%s,'schema.publish','schema',%s,%s)
    """, (actor, spec["id"], Jsonb({"source": "human", "version": version})))
    await notify(conn, "config", "config.changed", kind="schema", id=spec["id"], version=version)
