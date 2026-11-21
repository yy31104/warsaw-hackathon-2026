"""Human review of unknown input formats. Responses can also be inspected in /docs."""
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from alerting.assist.schema_suggest import NAMES, suggest
from alerting.clock import FixedClock, SYSTEM
from alerting.config.loader import read_snapshot
from alerting.contracts import Channel, Severity, Transport
from alerting.db import pool
from alerting.ingest.normalize import InvalidEvent, normalize
from alerting.rules.engine import Ruleset
from alerting.rules.dsl import number
from alerting.rules.safety import verify
from alerting.rules.stability import TIME_LIMIT_SECONDS, stability
from alerting.schemas.issues import index_legacy
from alerting.schemas.hypotheses import generate_hypotheses
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
    except InvalidEvent as exc:
        raise HTTPException(422, str(exc)) from exc


class RoutingExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipients: list[str] | None = None
    channels: list[Channel] | None = None

    @model_validator(mode="after")
    def require_expectation(self):
        if self.recipients is None and self.channels is None:
            raise ValueError("specify expected recipients or channels")
        return self


class VerificationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: dict
    expect: Literal["notify", "quiet"] | RoutingExpectation
    note: str | None = None
    source: str | None = None
    transport: Transport | None = None


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: dict = Field(alias="schema")
    samples: list[VerificationSample] = Field(min_length=1)


class StabilitySample(VerificationSample):
    expect: Literal["notify", "quiet"] | RoutingExpectation | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    value: str | int | float
    source: Literal["declared", "heuristic", "llm_suggestion"] = "declared"
    label: str | None = None


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1)
    kind: Literal["scale", "severity", "timezone", "alias"]
    path: str = Field(min_length=1)
    label: str | None = None
    candidates: list[Candidate] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_options(self):
        values = [choice.value for choice in self.candidates]
        if self.kind == "scale":
            values = [number(value) for value in values]
            if None in values:
                raise ValueError("scale candidates must be finite numbers")
        elif self.kind == "severity":
            if any(value not in Severity for value in values):
                raise ValueError("severity candidates must be canonical severity names")
        elif self.kind == "alias":
            if self.path not in NAMES or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("alias hypotheses need a canonical alias name and nonempty field paths")
        elif self.kind == "timezone":
            try:
                for value in values:
                    ZoneInfo(value)
            except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
                raise ValueError("timezone candidates must name valid IANA time zones") from exc
        if len(set(values)) != len(values):
            raise ValueError("hypothesis candidates must have distinct values")
        return self


class StabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: dict
    samples: list[StabilitySample] = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_axes(self):
        axes = [(h.kind, h.path, h.label if h.kind == "severity" else None) for h in self.hypotheses]
        if len(axes) != len(set(axes)):
            raise ValueError("each interpretation axis must be supplied once")
        return self


def _validate_draft(draft: dict, active: dict) -> dict:
    base = draft.get("base", draft.get("id"))
    inherited = active.get(base) if isinstance(base, str) else None
    if "base" in draft and inherited is None:
        raise ValueError("base must name an active schema")
    spec = {key: value for key, value in (inherited or {}).items() if key not in ("version", "origin")}
    spec.update({key: value for key, value in draft.items() if key != "base"})
    return SchemaDraft.model_validate(spec).spec()


@router.post("/schemas/verify")
async def verify_schema(request: VerificationInput, db_pool=Depends(pool)):
    """Run each sample with a fresh alert state using the active routing configuration."""
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ ONLY")
        snapshot = await read_snapshot(conn)
    ruleset = Ruleset(snapshot.rules, snapshot.schemas)
    sources = {source["id"]: source["transport"] for source in snapshot.sources}
    samples = [sample.model_dump(mode="json", exclude_none=True) for sample in request.samples]
    for sample in samples:
        if sample.get("source") in sources:
            sample.setdefault("transport", sources[sample["source"]])
    try:
        schema = _validate_draft(request.draft, ruleset.schemas)
        clock = FixedClock(SYSTEM.now())
        report = verify(request.draft, samples, ruleset, snapshot.directory, snapshot.runbooks, clock)
        for sample, result in zip(samples, report["samples"], strict=True):
            source = {"id": sample.get("source", "preview"), "transport": sample.get("transport", "http")}
            # The verifier exposes projected fields and routing. Publish the same
            # normalizer's diagnostics here alongside its existing response.
            event = normalize(sample["event"], source, [schema], received_at=clock.now())
            result["normalization"] = event.trace.get("normalization", {})
            result["problems"] = event.trace.get("problems", [])
            if "note" in sample:
                result["note"] = sample["note"]
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return report


def _stability_report(request, snapshot, clock):
    deadline = monotonic() + TIME_LIMIT_SECONDS
    draft = _validate_draft(request.draft, {schema["id"]: schema for schema in snapshot.schemas})
    transports = {source["id"]: source["transport"] for source in snapshot.sources}
    samples = [sample.model_dump(mode="json", exclude_none=True) for sample in request.samples]
    for sample in samples:
        if sample.get("source") in transports:
            sample.setdefault("transport", transports[sample["source"]])
    declared = [item.model_dump(mode="json", exclude_none=True) for item in request.hypotheses]
    hypotheses = generate_hypotheses(draft, samples, snapshot, declared, clock=clock)
    return stability(samples, draft, hypotheses, snapshot, clock=clock, deadline=deadline)


@router.post("/schemas/stability")
async def schema_stability(request: StabilityInput, db_pool=Depends(pool)):
    """Compare draft interpretations against a single committed configuration, without publishing."""
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = await read_snapshot(conn)
    try:
        return await run_in_threadpool(_stability_report, request, snapshot, FixedClock(SYSTEM.now()))
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    except (ValueError, ArithmeticError, RecursionError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/schemas")
async def confirm_schema(draft: SchemaDraft, fingerprint: str | None = None,
                         base_version: int | None = Query(None, ge=0),
                         x_user: str | None = Header(None), db_pool=Depends(pool)):
    """Publish a reviewed mapping and requeue matching events using their original IDs."""
    if not x_user:
        raise HTTPException(401, "Set X-User to the reviewing team member")
    try:
        async with db_pool.connection() as conn:
            directory = (await read_snapshot(conn)).directory
            team = next((t for t in directory["teams"] if t["id"] == directory["fallback_team"]), {})
            if x_user not in {*team.get("members", []), *team.get("approvers", [])}:
                raise HTTPException(403, "Schema reviews belong to the configured fallback team")
            return await publish(conn, draft.spec(), fingerprint, x_user, base_version)
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except InvalidEvent as exc:
        raise HTTPException(422, str(exc)) from exc
