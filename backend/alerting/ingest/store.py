"""Write one inbox batch atomically. All transports use the same identity constraint."""
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from alerting.contracts import CanonicalEvent, Outcome
from alerting.ingest.normalize import storage_key
from alerting.schemas.issues import register_unknowns


@dataclass(frozen=True)
class RejectedInput:
    source_id: str
    source_event_id: str | None
    reason: str


@dataclass(frozen=True)
class IngestResult:
    outcome: Outcome
    event_id: int | None
    reason: str | None = None


# Allocate IDs on the server so RETURNING also maps unkeyed rows back to their
# input positions. Sorting locks by identity avoids opposite-order batch deadlocks.
_INSERT = """
    WITH batch AS MATERIALIZED (
        SELECT pos, doc, nextval(pg_get_serial_sequence('events', 'id')) AS proposed_id
        FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS input(doc, pos)
    ), inserted AS (
        INSERT INTO events (id, source_id, transport, source_event_id, payload_hash, schema_id,
                            schema_version, schema_name, severity, service, environment, occurred_at,
                            received_at, raw, fields, lane, status, trace)
        SELECT b.proposed_id, e.source_id, e.transport, e.source_event_id, e.payload_hash, e.schema_id,
               e.schema_version, e.schema_name, e.severity, e.service, e.environment, e.occurred_at,
               e.received_at, e.raw, e.fields, e.lane, e.status, e.trace
        FROM batch b CROSS JOIN LATERAL jsonb_populate_record(NULL::events, b.doc) e
        ORDER BY e.source_id, e.source_event_id, b.pos
        ON CONFLICT (source_id, source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING
        RETURNING id
    )
    SELECT b.pos - 1, i.id FROM batch b JOIN inserted i ON i.id = b.proposed_id
"""


async def _store_events(conn, events: list[CanonicalEvent]) -> list[IngestResult]:
    if not events:
        return []
    rows = [event.model_dump(mode="json", exclude={"id"}) for event in events]
    inserted = dict(await (await conn.execute(_INSERT, (Jsonb(rows),))).fetchall())
    collisions = {(e.source_id, e.source_event_id) for i, e in enumerate(events) if i not in inserted}
    existing = {}
    if collisions:
        sources, keys = zip(*collisions)
        # A fresh READ COMMITTED statement also sees a concurrent winner that the
        # INSERT had to wait for. A lookup in the same SQL statement would miss it.
        cursor = await conn.execute("""
            SELECT e.source_id, e.source_event_id, e.id, e.payload_hash
            FROM events e JOIN unnest(%s::text[], %s::text[]) AS k(source_id, source_event_id)
            USING (source_id, source_event_id)
        """, (list(sources), list(keys)))
        existing = {(source, key): (id_, digest) for source, key, id_, digest in await cursor.fetchall()}

    results = []
    for index, event in enumerate(events):
        if index in inserted:
            outcome = Outcome.NEEDS_REVIEW if event.status == "needs_review" else Outcome.ACCEPTED
            result = IngestResult(outcome, inserted[index])
        else:
            event_id, digest = existing[event.source_id, event.source_event_id]
            if digest == event.payload_hash:
                result = IngestResult(Outcome.DUPLICATE, event_id, "same identity and payload")
            else:
                result = IngestResult(Outcome.CONFLICT, event_id, "same identity with a different payload")
        results.append(result)
    return results


async def save_batch(conn: AsyncConnection, inputs: list[CanonicalEvent | RejectedInput]) -> list[IngestResult]:
    """Return one outcome per input, in order. Rejections and accepted rows commit together."""
    async with conn.transaction():
        stored = iter(await _store_events(conn, [item for item in inputs if isinstance(item, CanonicalEvent)]))
        results = []
        audit = []
        unknowns = []
        for item in inputs:
            if isinstance(item, RejectedInput):
                result = IngestResult(Outcome.REJECTED, None, item.reason)
                key = storage_key(item.source_event_id)
            else:
                result = next(stored)
                key = item.source_event_id
                if result.outcome == Outcome.NEEDS_REVIEW:
                    unknowns.append(item.model_copy(update={"id": result.event_id}))
            results.append(result)
            if result.reason:
                audit.append({"source_id": item.source_id, "source_event_id": key,
                              "kind": result.outcome.value, "reason": result.reason, "existing_event_id": result.event_id})
        if audit:
            await conn.execute("""
                INSERT INTO ingest_rejections (source_id, source_event_id, kind, reason, existing_event_id)
                SELECT source_id, source_event_id, kind, reason, existing_event_id
                FROM jsonb_populate_recordset(NULL::ingest_rejections, %s)
            """, (Jsonb(audit),))
        await register_unknowns(conn, unknowns)
    return results
