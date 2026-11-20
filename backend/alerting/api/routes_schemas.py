"""Human review of unknown input formats. Responses can also be inspected in /docs."""
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.rows import dict_row
from pydantic import BaseModel, model_validator

from alerting.assist.schema_suggest import suggest
from alerting.clock import SYSTEM
from alerting.config import files
from alerting.db import pool
from alerting.ingest.normalize import InvalidEvent, normalize
from alerting.schemas.issues import index_legacy
from alerting.schemas.mapping import SchemaDraft
from alerting.schemas.registry import ReviewError, lock_registry, schemas, with_draft
from alerting.schemas.review import preview_event, publish, sample_event

router = APIRouter(prefix="/v1", tags=["schemas"])


@router.get("/schema-issues")
async def list_issues(status: Literal["open", "resolved", "ignored", "all"] = "open",
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                      db_pool=Depends(pool)):
    async with db_pool.connection() as conn, conn.transaction():
        legacy = await conn.execute("SELECT 1 FROM events WHERE status='needs_review' AND review_fingerprint IS NULL LIMIT 1")
        if await legacy.fetchone():
            await lock_registry(conn, write=True)
            await index_legacy(conn)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute("""
                SELECT fingerprint, source_id, sample_event_id, sample_fields, event_count, status,
                       first_seen_at, last_seen_at, resolved_schema_id
                FROM schema_issues WHERE %s='all' OR status=%s
                ORDER BY last_seen_at DESC, fingerprint LIMIT %s OFFSET %s
            """, (status, status, limit, offset))
            return {"items": await cursor.fetchall()}


@router.get("/schemas")
async def list_schemas(db_pool=Depends(pool)):
    async with db_pool.connection() as conn:
        return {"items": await schemas(conn)}


class SuggestionInput(BaseModel):
    sample: dict | None = None
    fingerprint: str | None = None

    @model_validator(mode="after")
    def select_one(self):
        if (self.sample is None) == (self.fingerprint is None):
            raise ValueError("provide one sample or one review fingerprint")
        return self


@router.post("/schemas/suggest")
async def suggest_schema(request: SuggestionInput, db_pool=Depends(pool)):
    """Return an editable draft from one sample or the current example in a review issue."""
    if request.fingerprint is not None:
        try:
            async with db_pool.connection() as conn:
                sample = await sample_event(conn, request.fingerprint)
        except ReviewError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
    else:
        try:
            sample = normalize(request.sample, {"id": "sample", "transport": "http"}, [], received_at=SYSTEM.now())
        except InvalidEvent as exc:
            raise HTTPException(422, str(exc)) from exc
    # The remote helper, if enabled, runs after the sample's DB connection is released.
    return await suggest(sample)


@router.post("/schemas/preview")
async def preview_schema(draft: SchemaDraft, fingerprint: str, db_pool=Depends(pool)):
    """Show the projected fields of the selected issue's sample without changing any records."""
    spec = draft.spec()
    try:
        async with db_pool.connection() as conn, conn.transaction():
            await lock_registry(conn)
            active = await schemas(conn)
            candidate, version, changed = with_draft(active, spec)
            result = preview_event(await sample_event(conn, fingerprint), candidate, spec)
            return {**result, "base_version": version - int(changed)}
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/schemas")
async def confirm_schema(draft: SchemaDraft, fingerprint: str | None = None,
                         base_version: int | None = Query(None, ge=0),
                         x_user: str | None = Header(None), db_pool=Depends(pool)):
    """Publish a reviewed mapping and requeue matching events using their original IDs."""
    if not x_user:
        raise HTTPException(401, "Set X-User to the reviewing team member")
    directory = files.directory()
    team = next((t for t in directory["teams"] if t["id"] == directory["fallback_team"]), {})
    if x_user not in {*team.get("members", []), *team.get("approvers", [])}:
        raise HTTPException(403, "Schema reviews belong to the configured fallback team")
    try:
        async with db_pool.connection() as conn:
            return await publish(conn, draft.spec(), fingerprint, x_user, base_version)
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
