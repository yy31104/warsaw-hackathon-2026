"""HTTP input and inspection of a stored event (CONTRACT §9)."""
import json
from hmac import compare_digest
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.contracts import Outcome
from alerting.db import pool
from alerting.ingest.formats import BodyError, FIX_EXAMPLES, decode_body, media_format
from alerting.ingest.inputs import prepare
from alerting.ingest.normalize import MAX_BYTES, flatten
from alerting.ingest.store import RejectedInput, save_batch
from alerting.schemas.registry import ingest_registry

router = APIRouter(prefix="/v1", tags=["ingest"])


def _event_key(key: str | None, batch_index: int | None) -> str | None:
    if not key or not key.strip():
        return None
    if batch_index is not None:
        return f"http-batch:{batch_index}:{key}"
    if key.startswith(("http-batch:", "http-key:")):
        return "http-key:" + key
    return key


def _body_error(error: BodyError) -> JSONResponse:
    if error.format == "json":
        # Keep the existing JSON error envelope and status.
        return JSONResponse({"detail": f"{error}. Check field {error.field}. For example, send {error.fix}."},
                            status_code=error.status)
    content = {"detail": str(error), "field": error.field, "fix_example": error.fix}
    if error.line is not None:
        content["line"] = error.line
    if error.tried:
        content.update(tried=error.tried, hint=error.fix)
    return JSONResponse(content, status_code=error.status)


def _rejections(decoded, results):
    rejected = []
    for index, (row, result) in enumerate(zip(decoded.rows, results, strict=True)):
        if result.outcome != Outcome.REJECTED:
            continue
        issue = {"index": index, "reason": result.reason}
        if decoded.format != "json":
            issue.update(row=index + 1, fix_example=FIX_EXAMPLES[decoded.format])
            if row.line is not None:
                issue["line"] = row.line
        rejected.append(issue)
    return rejected


@router.post("/events/{source_id}", status_code=202)
async def ingest(source_id: str, request: Request, x_api_key: str | None = Header(None),
                 idempotency_key: str | None = Header(None), db_pool=Depends(pool)):
    source = next((s for s in files.sources() if s["id"] == source_id and s["transport"] == "http"), None)
    if source is None:
        raise HTTPException(404, "unknown webhook source")
    if not source.get("api_key") or not compare_digest((x_api_key or "").encode(), source["api_key"].encode()):
        raise HTTPException(401, "invalid API key")

    received_at = SYSTEM.now()
    body = bytearray()
    content_type = request.headers.get("content-type")
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BYTES:
                raise BodyError("body exceeds 256 KiB", media_format(content_type), status=413)
            body.extend(chunk)
        decoded = decode_body(body, content_type, source)
    except BodyError as exc:
        async with db_pool.connection() as conn:
            await save_batch(conn, [RejectedInput(source_id, idempotency_key, str(exc))])
        return _body_error(exc)

    async with ingest_registry(db_pool, files.schemas()) as (conn, schemas):
        if decoded.format == "text" and not source.get("parser"):
            schemas = []  # Bare text supplies no trusted fields for automatic routing.
        inputs = []
        for index, row in enumerate(decoded.rows):
            key = _event_key(idempotency_key, index if decoded.batch else None)
            if row.reason is not None:
                inputs.append(RejectedInput(source_id, key, row.reason))
            else:
                inputs.append(prepare(row.value, source, schemas, message_id=key, headers=request.headers,
                                      received_at=received_at))
        results = await save_batch(conn, inputs)
    rejected = _rejections(decoded, results)
    response = {
        "accepted": sum(r.outcome in (Outcome.ACCEPTED, Outcome.NEEDS_REVIEW) for r in results),
        "duplicates": sum(r.outcome == Outcome.DUPLICATE for r in results),
        "conflicts": sum(r.outcome == Outcome.CONFLICT for r in results),
        "rejected": rejected,
        "event_ids": [r.event_id for r in results if r.event_id is not None],
    }
    if decoded.format != "json" and rejected and len(rejected) == len(results):
        return JSONResponse({"detail": "Every record was rejected", **response}, status_code=400)
    return response


def _filter_value(value: str):
    try:
        try:
            expected = json.loads(value)
        except json.JSONDecodeError:
            expected = value
        # Apply the same structural and NUL limits as a stored value. Check
        # JSON encoding too, since escaped surrogates and nonfinite numbers
        # would otherwise fail inside PostgreSQL.
        flatten({"value": expected})
        json.dumps(expected, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, "invalid field value") from exc
    return expected


@router.get("/events")
async def list_events(
    source: str | None = Query(None, min_length=1, max_length=256),
    schema: str | None = Query(None, min_length=1, max_length=256, description="Exact schema ID or rendered name"),
    status: Literal["pending", "processed", "needs_review", "rejected", "quarantined"] | None = None,
    field: str | None = Query(None, max_length=4096, description="Literal flattened path or alias"),
    value: str | None = Query(None, max_length=8192, description="JSON value or plain text. Omit for a field existence check."),
    limit: int = Query(20, ge=1, le=100),
    cursor: int | None = Query(None, ge=1, le=2**63 - 1, description="Return IDs below this cursor"),
    offset: int | None = Query(None, ge=0, le=100_000, description="Skip this many matches instead of using a cursor"),
    db_pool=Depends(pool),
):
    """Return stored events by descending ID. Page with a cursor or offset using the same filters."""
    if cursor is not None and offset is not None:
        raise HTTPException(422, "use either cursor or offset")
    if value is not None and field is None:
        raise HTTPException(422, "value requires field")
    if any(part is not None and "\0" in part for part in (source, schema, field)):
        raise HTTPException(422, "filters cannot contain NUL characters")

    predicates = []
    params = []
    for predicate, term in (("e.source_id = %s", source), ("e.status = %s", status), ("e.id < %s", cursor)):
        if term is not None:
            predicates.append(predicate)
            params.append(term)
    if schema is not None:
        predicates.append("(e.schema_id = %s OR e.schema_name = %s)")
        params.extend((schema, schema))
    if field is not None:
        if value is None:
            predicates.append("e.fields ? %s")
            params.append(field)
        else:
            expected = _filter_value(value)
            # Containment uses events_fields_gin. JSONB equality also checks
            # array order and rejects proper subsets of stored objects.
            predicates.append("e.fields @> %s AND e.fields -> %s::text = %s")
            params.extend((Jsonb({field: expected}), field, Jsonb(expected)))

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    async with db_pool.connection() as conn:
        rows = await (await conn.execute(f"""
            SELECT to_jsonb(e) FROM events e {where}
            ORDER BY e.id DESC LIMIT %s OFFSET %s
        """, (*params, limit + 1, offset or 0))).fetchall()
    events = [row[0] for row in rows[:limit]]
    return {"items": events, "next_cursor": events[-1]["id"] if len(rows) > limit else None}


@router.get("/events/{event_id}")
async def get_event(event_id: int, db_pool=Depends(pool)):
    async with db_pool.connection() as conn:
        row = await (await conn.execute("SELECT to_jsonb(e) FROM events e WHERE e.id=%s", (event_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "event not found")
    return row[0]
