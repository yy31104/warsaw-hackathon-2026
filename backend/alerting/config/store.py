"""SQL helpers for transactional, append-only config versions."""

from dataclasses import dataclass
import hashlib
import json

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM


class ConfigError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ConfigObject:
    kind: str
    id: str
    version: int
    origin: str
    spec: dict
    content_hash: str
    active: bool
    position: int | None

    def public(self):
        return {**self.spec, "id": self.id, "version": self.version,
                "origin": "file" if self.origin == "file" else "ui"}


def content_hash(spec):
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


async def lock_writes(conn):
    # Covers both new IDs and existing versions, and the directory used for permission checks.
    await conn.execute("select pg_advisory_xact_lock('config_objects'::regclass::oid::int, 202)")


async def get_object(conn, kind, id):
    async with conn.cursor(row_factory=class_row(ConfigObject)) as cur:
        await cur.execute("""
            select kind,id,version,origin,spec,content_hash,active,position
            from config_objects where kind=%s and id=%s order by version desc limit 1
        """, (kind, id))
        return await cur.fetchone()


async def audit(conn, actor, action, kind, id, detail):
    await conn.execute("""
        insert into audit_log (actor,action,target_kind,target_id,detail) values (%s,%s,%s,%s,%s)
    """, (actor, action, kind, str(id), Jsonb(detail)))


async def notify(conn, kind, id, version, *, team_id=None, active=True):
    identity = {"kind": kind, "id": id, "version": version}
    data = {**identity, "team_id": team_id, "active": active, "ts": SYSTEM.now().isoformat()}
    await conn.execute("select pg_notify('config', %s)", (json.dumps({
        **identity, "type": "config.changed", "data": data,
    }),))


async def write_object(conn, kind, id, spec, *, current, origin, actor, team_id=None,
                       position=None, initial_version=1, audit_source=None):
    """Publish under the config write lock held by the caller."""
    if current and (current.origin == "file") != (origin == "file"):
        raise ConfigError(409, f"{kind} '{id}' is managed by {current.origin} configuration")
    digest = content_hash(spec)
    if current and current.content_hash == digest:
        if not current.active or current.position != position:
            await conn.execute("""
                update config_objects set active=true, position=%s
                where kind=%s and id=%s and version=%s
            """, (position, kind, id, current.version))
            await notify(conn, kind, id, current.version, team_id=team_id)
        return current.public()

    version = initial_version if current is None else current.version + 1
    if current and current.active:
        await conn.execute("update config_objects set active=false where kind=%s and id=%s and active", (kind, id))
    await conn.execute("""
        insert into config_objects (kind,id,version,spec,content_hash,origin,created_by,position)
        values (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (kind, id, version, Jsonb(spec), digest, origin, actor, position))
    await audit(conn, actor, f"{kind}.publish", kind, id, {
        "version": version, "source": audit_source or ("file" if origin == "file" else "human"),
    })
    await notify(conn, kind, id, version, team_id=team_id)
    return {**spec, "id": id, "version": version, "origin": origin}


def require_current_version(current, expected):
    if expected is not None and (current is None or current.version != expected):
        raise ConfigError(409, "Configuration version changed. Reload the current version and try again.")
