"""Versioned schema registry shared by HTTP, AMQP and file imports."""
import hashlib
import json
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb

from alerting.config import files
from alerting.ingest.normalize import compile_schemas
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
    cursor = await conn.execute("""
        WITH active_schemas AS (
            SELECT active.spec, active.version, active.origin, active.position, active.id,
                   coalesce(first.created_at, active.created_at) AS created_at
            FROM config_objects active
            LEFT JOIN config_objects first ON (first.kind, first.id)=(active.kind, active.id) AND first.version=1
            WHERE active.kind='schema' AND active.active
        )
        SELECT state.files_imported, s.spec, s.version, s.origin
        FROM config_state state
        LEFT JOIN active_schemas s ON state.files_imported OR s.origin <> 'file'
        WHERE state.singleton
        ORDER BY (s.origin='file') DESC NULLS LAST,
                 CASE WHEN s.origin='file' THEN s.position END, s.created_at, s.id
    """)
    rows = await cursor.fetchall()
    base = []
    if not rows or not rows[0][0]:
        base = files.schemas() if file_specs is None else file_specs
    ordered = [{**s, "version": s.get("version", 1), "origin": "file"} for s in base]
    file_ids = {s["id"] for s in base}
    for _, spec, version, origin in rows:
        if spec is not None and spec["id"] not in file_ids:
            ordered.append({**spec, "version": version, "origin": origin})
    return ordered


@asynccontextmanager
async def ingest_registry(pool, file_specs):
    async with pool.connection() as conn, conn.transaction():
        await lock_registry(conn)
        yield conn, compile_schemas(await schemas(conn, file_specs))


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
