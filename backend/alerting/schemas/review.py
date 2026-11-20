"""Preview a mapping on its stored example and release matching original inbox rows."""
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from alerting.contracts import CanonicalEvent
from alerting.ingest.normalize import reclassify
from alerting.schemas.issues import index_legacy, refresh_issue
from alerting.schemas.registry import ReviewError, lock_registry, schemas, with_draft, write_version
from alerting.templates import lookup, variables


async def sample_event(conn, shape) -> CanonicalEvent:
    cursor = await conn.execute("""
        SELECT to_jsonb(e) FROM schema_issues i JOIN events e ON e.id=i.sample_event_id
        WHERE i.fingerprint=%s
    """, (shape,))
    row = await cursor.fetchone()
    if row is None:
        raise ReviewError(404, "Unknown review fingerprint")
    return CanonicalEvent.model_validate(row[0])


def preview_event(sample, candidate, draft):
    projected = reclassify(sample, candidate)
    return {
        "event_id": sample.id,
        "matches": projected.schema_id == draft["id"],
        "schema_id": projected.schema_id,
        "lane": projected.lane,
        "aliases": {key: projected.fields[key] for key in (
            "$source", "$transport", "$schema", "$severity", "$service", "$environment", "$error_code", "$message", "$time")},
        "missing_paths": sorted({p for p in draft.get("aliases", {}).values() if sample.fields.get(p) is None}),
    }


async def _replay(conn, candidate, spec):
    schema_id = spec["id"]
    identity_paths = variables(spec.get("event_id"))
    last_id = total = 0
    touched = set()
    while True:
        cursor = await conn.execute("""
            SELECT to_jsonb(e), review_fingerprint FROM events e
            WHERE status='needs_review' AND id>%s ORDER BY id LIMIT 200 FOR UPDATE
        """, (last_id,))
        rows = await cursor.fetchall()
        if not rows:
            break
        updates = []
        for record, shape in rows:
            last_id = record["id"]
            event = reclassify(CanonicalEvent.model_validate(record), candidate)
            if event.schema_id == schema_id:
                if event.source_event_id != record["source_event_id"]:
                    for path in identity_paths:
                        value = lookup(event.fields, path)
                        if isinstance(value, str) and ("<redacted>" in value or "<email>" in value):
                            raise ReviewError(409, "A proposed event identity uses a masked field. Choose a retained identifier.")
                updates.append(event.model_dump(mode="json", include={
                    "id", "schema_id", "schema_version", "schema_name", "source_event_id", "severity",
                    "service", "environment", "occurred_at", "fields", "lane"}))
                touched.add(shape)
        if updates:
            # The original hash, timestamps, trace, raw JSON and existing transport
            # identity survive replay. A derived key can replace the opt-in hash heuristic.
            await conn.execute("""
                UPDATE events e SET schema_id=v.schema_id, schema_version=v.schema_version,
                    schema_name=v.schema_name, source_event_id=v.source_event_id,
                    severity=v.severity, service=v.service, environment=v.environment,
                    occurred_at=v.occurred_at, fields=v.fields, lane=v.lane, status='pending'
                FROM jsonb_populate_recordset(NULL::events,%s) v
                WHERE e.id=v.id AND e.status='needs_review'
            """, (Jsonb(updates),))
            total += len(updates)
    for shape in sorted(touched):
        await refresh_issue(conn, shape, schema_id)
    return total


async def publish(conn, spec, shape, actor, base_version=None):
    try:
        async with conn.transaction():
            await lock_registry(conn, write=True)
            await index_legacy(conn)
            active = await schemas(conn)
            candidate, version, changed = with_draft(active, spec)
            if changed and base_version is not None and base_version != version - 1:
                raise ReviewError(409, "The schema changed since the preview; reload its current version")
            if shape is not None:
                sample = await sample_event(conn, shape)
                if changed and not preview_event(sample, candidate, spec)["matches"]:
                    raise ReviewError(422, "This draft does not select the reviewed sample under the active detection order")
            if changed:
                await write_version(conn, spec, version, actor)
            count = await _replay(conn, candidate, spec)
    except UniqueViolation as exc:
        if exc.diag.constraint_name != "events_identity":
            raise
        raise ReviewError(409, "The event_id mapping gives two retained events the same identity. Choose a more specific key.") from exc
    return {"id": spec["id"], "version": version, "origin": "ui", "reprocessed": count}
