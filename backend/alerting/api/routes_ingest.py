"""HTTP input and inspection of a stored event (CONTRACT §9)."""
from hmac import compare_digest

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.contracts import Outcome
from alerting.db import pool
from alerting.ingest.inputs import prepare
from alerting.ingest.normalize import MAX_BYTES, InvalidEvent, decode_json
from alerting.ingest.store import RejectedInput, save_batch
from alerting.templates import lookup

router = APIRouter(prefix="/v1", tags=["ingest"])


def _event_key(key: str | None, batch_index: int | None) -> str | None:
    if not key or not key.strip():
        return None
    if batch_index is not None:
        return f"http-batch:{batch_index}:{key}"
    if key.startswith(("http-batch:", "http-key:")):
        return "http-key:" + key
    return key


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
    error_status = 400
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BYTES:
                error_status = 413
                raise InvalidEvent("body exceeds 256 KiB")
            body.extend(chunk)
        payload = decode_json(body)
        if isinstance(payload, dict) and (path := source.get("split_path")):
            children = lookup(payload, path)
            if children is not None or path in payload:
                if not isinstance(children, list):
                    raise InvalidEvent("split_path must contain an array")
                payload = children
    except InvalidEvent as exc:
        async with db_pool.connection() as conn:
            await save_batch(conn, [RejectedInput(source_id, idempotency_key, str(exc))])
        raise HTTPException(error_status, str(exc)) from exc

    schemas = files.schemas()
    batch = isinstance(payload, list)
    inputs = [prepare(item, source, schemas, message_id=_event_key(idempotency_key, i if batch else None),
                      headers=request.headers, received_at=received_at)
              for i, item in enumerate(payload if batch else [payload])]
    async with db_pool.connection() as conn:
        results = await save_batch(conn, inputs)
    return {
        "accepted": sum(r.outcome in (Outcome.ACCEPTED, Outcome.NEEDS_REVIEW) for r in results),
        "duplicates": sum(r.outcome == Outcome.DUPLICATE for r in results),
        "conflicts": sum(r.outcome == Outcome.CONFLICT for r in results),
        "rejected": [{"index": i, "reason": r.reason} for i, r in enumerate(results) if r.outcome == Outcome.REJECTED],
        "event_ids": [r.event_id for r in results if r.event_id is not None],
    }


@router.get("/events/{event_id}")
async def get_event(event_id: int, db_pool=Depends(pool)):
    async with db_pool.connection() as conn:
        row = await (await conn.execute("SELECT to_jsonb(e) FROM events e WHERE e.id=%s", (event_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "event not found")
    return row[0]
