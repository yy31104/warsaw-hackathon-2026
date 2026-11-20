"""Import the file baseline and read coherent snapshots of the committed registry."""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from weakref import WeakKeyDictionary

import psycopg
from watchfiles import awatch
import yaml

from alerting.config import files
from alerting.config.store import ConfigError, content_hash, get_object, lock_writes, notify, write_object
from alerting.config.validation import validate_json, validate_match
from alerting.db import pool
from alerting.schemas.registry import lock_registry

log = logging.getLogger(__name__)
FILE_POLL_SECONDS = 0.5


@dataclass
class FileImport:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    signature: tuple | None = None
    next_check: float = 0


_imports = WeakKeyDictionary()
_compiled = WeakKeyDictionary()


def file_signature():
    root = Path(files.CONFIG_DIR)
    stats = ((path, path.stat()) for path in sorted(root.rglob("*")) if path.is_file())
    return tuple((str(path), stat.st_mtime_ns, stat.st_size) for path, stat in stats)


def read_files():
    directory = files.directory()
    return {
        "source": files.sources(),
        "schema": files.schemas(),
        "rule": files.rules(),
        "team": directory.get("teams", []),
        "user": directory.get("users", []),
        "runbook": files.runbooks(),
        "fallback_team": [{"id": "default", "value": directory.get("fallback_team")}],
        "default_preferences": [{"id": "default", "value": directory.get("default_preferences", [])}],
    }


def file_spec(spec):
    return {key: value for key, value in spec.items() if key not in ("origin", "version")}


def file_entries():
    entries = {}
    for kind, specs in read_files().items():
        for position, raw in enumerate(specs):
            spec = validate_json(file_spec(raw))
            id = spec["id"]
            if not isinstance(id, str) or not id:
                raise ValueError(f"Each {kind} needs a nonempty ID")
            if (kind, id) in entries:
                raise ValueError(f"Duplicate {kind} '{id}' in configuration files")
            if kind == "schema":
                validate_match(spec["detect"])
                if spec.get("resolve_when") is not None:
                    validate_match(spec["resolve_when"])
            version = raw.get("version", 1)
            if type(version) is not int or version < 1:
                raise ValueError(f"Invalid starting version for {kind} '{id}'")
            entries[kind, id] = spec, position, version, content_hash(raw)
    return entries


async def import_files(conn, entries):
    # Keep Yi's pre-registry rule version identity for existing alerts and installs.
    rows = await conn.execute("select rule_id,content_hash,version from backend_file_rule_revisions")
    previous_rules = {id: (digest, version) for id, digest, version in await rows.fetchall()}
    for (kind, id), (spec, position, initial_version, legacy_digest) in entries.items():
        current = await get_object(conn, kind, id)
        if current and current.origin != "file":
            log.warning("Ignoring file %s %r because its ID already belongs to UI configuration", kind, id)
            continue
        if kind == "rule" and id in previous_rules:
            old_digest, old_version = previous_rules[id]
            initial_version = max(initial_version, old_version + int(legacy_digest != old_digest))
        saved = await write_object(conn, kind, id, spec, current=current, origin="file", actor="system",
                                   position=position, initial_version=initial_version)
        if kind == "rule" and previous_rules.get(id) != (legacy_digest, saved["version"]):
            await conn.execute("""
                insert into backend_file_rule_revisions (rule_id,content_hash,version) values (%s,%s,%s)
                on conflict(rule_id) do update set content_hash=excluded.content_hash,version=excluded.version
            """, (id, legacy_digest, saved["version"]))

    rows = await conn.execute("select kind,id,version from config_objects where active and origin='file'")
    for kind, id, version in await rows.fetchall():
        if (kind, id) not in entries:
            await conn.execute("""
                update config_objects set active=false where kind=%s and id=%s and version=%s
            """, (kind, id, version))
            await notify(conn, kind, id, version, active=False)
    await conn.execute("update config_state set files_imported=true, revision=revision+1 where not files_imported")


async def sync_files(db, *, force=False):
    state = _imports.setdefault(db, FileImport())
    if not force and monotonic() < state.next_check:
        return
    async with state.lock:
        if not force and monotonic() < state.next_check:
            return
        try:
            signature = file_signature()
            if state.signature != signature:
                entries = file_entries()
                async with db.connection() as conn, conn.transaction():
                    await lock_writes(conn)
                    await lock_registry(conn, write=True)
                    if file_signature() != signature:
                        # A newer edit arrived before we obtained the import lock.
                        return
                    await import_files(conn, entries)
                state.signature = signature
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            if state.signature is None and not await files_previously_imported(db):
                raise
            log.exception("Keeping the last committed configuration after a file import error")
        state.next_check = monotonic() + FILE_POLL_SECONDS


async def files_previously_imported(db):
    async with db.connection() as conn:
        row = await conn.execute("select files_imported from config_state where singleton")
        return (await row.fetchone())[0]


async def _watch(db):
    async for _ in awatch(str(files.CONFIG_DIR), debounce=350, step=50):
        try:
            await sync_files(db, force=True)
        except psycopg.Error:
            log.exception("Could not import updated configuration files")


@asynccontextmanager
async def watch_config(db):
    await sync_files(db, force=True)
    task = asyncio.create_task(_watch(db))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@dataclass
class ConfigSnapshot:
    revision: int
    imported: bool
    rules: list
    directory: dict
    runbooks: list
    schemas: list
    sources: list


def build_snapshot(revision, imported, objects):
    collections = {} if imported else {
        kind: [{**file_spec(spec), "origin": "file", "version": spec.get("version", 1)} for spec in specs]
        for kind, specs in read_files().items()
    }
    file_ids = {(kind, spec["id"]) for kind, specs in collections.items() for spec in specs}
    for kind, id, version, origin, spec in objects:
        if (kind, id) not in file_ids:
            collections.setdefault(kind, []).append({
                **spec, "id": id, "version": version, "origin": "file" if origin == "file" else "ui",
            })
    users = collections.get("user", [])
    preferences = {entry["id"]: entry["preferences"] for entry in collections.get("user_preferences", [])}
    directory = {
        "users": [{**user, "preferences": preferences[user["id"]]} if user["id"] in preferences else user
                  for user in users],
        "teams": collections.get("team", []),
        "fallback_team": next((row["value"] for row in collections.get("fallback_team", [])), None),
        "default_preferences": next((row["value"] for row in collections.get("default_preferences", [])), []),
    }
    return ConfigSnapshot(revision, imported, collections.get("rule", []), directory,
                          collections.get("runbook", []), collections.get("schema", []), collections.get("source", []))


async def read_snapshot(conn, *, known_revision=None):
    """Read only. A matching committed revision can reuse a worker's compiled settings."""
    cursor = await conn.execute("""
        select revision, files_imported, case when files_imported and revision=%s then null else (
            select coalesce(jsonb_agg(jsonb_build_array(a.kind,a.id,a.version,a.origin,a.spec)
                order by a.kind, a.origin<>'file', a.position nulls last, first.created_at, a.id), '[]'::jsonb)
            from config_objects a left join config_objects first
                on first.kind=a.kind and first.id=a.id and first.version=1
            where a.active
        ) end
        from config_state where singleton
    """, (known_revision,))
    revision, imported, objects = await cursor.fetchone()
    return None if objects is None else build_snapshot(revision, imported, objects)


async def read_settings(db, conn):
    from alerting.rules.storage import Settings
    from alerting.rules.engine import Ruleset

    cached = _compiled.get(db)
    result = await read_snapshot(conn, known_revision=cached[0] if cached else None)
    if result is None:
        return cached[1]
    settings = Settings(Ruleset(result.rules, result.schemas), result.directory, result.runbooks)
    if result.imported and (cached is None or cached[0] <= result.revision):
        _compiled[db] = result.revision, settings
    return settings


async def snapshot(db=None):
    db = db if db is not None else pool()
    await sync_files(db)
    async with db.connection() as conn:
        return await read_snapshot(conn)


async def sources(db=None):
    return (await snapshot(db)).sources


async def schemas(db=None):
    return (await snapshot(db)).schemas


async def rules(db=None):
    return (await snapshot(db)).rules


async def directory(db=None):
    return (await snapshot(db)).directory


async def runbooks(db=None):
    return (await snapshot(db)).runbooks
