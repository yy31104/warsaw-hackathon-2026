"""Read-only rule drafting, evaluation and historical impact endpoints."""

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from alerting.assist.rule_draft import draft_rule
from alerting.assist.text_input import TextInput
from alerting.clock import FixedClock, SYSTEM
from alerting.config import files
from alerting.contracts import CanonicalEvent, Channel, Severity, Transition
from alerting.db import pool
from alerting.ingest.normalize import detect_schema, flatten, normalize
from alerting.routing import plan
from alerting.rules.dsl import compile as compile_match
from alerting.rules.engine import Ruleset, evaluate
from alerting.rules.safety import check
from alerting.rules.suggestions import sample_filters, unknown_fields
from alerting.schemas.registry import schemas as active_schemas
from alerting.simulate.history import alert_snapshot, preview_history


router = APIRouter(tags=["rules"])


class PreviewRequest(BaseModel):
    draft: dict
    last_n: int = Field(default=200, ge=1, le=2000)


class TestRequest(BaseModel):
    rule: dict
    event: dict


class CheckRequest(BaseModel):
    rule: dict | None = None
    rule_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def one_rule(self):
        if (self.rule is None) == (self.rule_id is None):
            raise ValueError("provide rule or rule_id")
        return self


@asynccontextmanager
async def _read_only(db):
    async with db.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield conn


async def _config(conn):
    try:
        from alerting.config.loader import read_snapshot
    except ModuleNotFoundError as exc:
        if exc.name != "alerting.config.loader":
            raise
    else:
        return await read_snapshot(conn)

    # A Yi-only checkout has the schema registry and file settings but no config loader.
    rows = await conn.execute("SELECT id, spec, version FROM config_objects WHERE kind = 'rule' AND active ORDER BY id")
    rules = {rule["id"]: rule for rule in files.rules()}
    rules.update({id: {**spec, "id": id, "version": version} for id, spec, version in await rows.fetchall()})
    return SimpleNamespace(rules=list(rules.values()), schemas=await active_schemas(conn),
                           directory=files.directory(), runbooks=files.runbooks())


def _rule_from(config, rule_id):
    return next((rule for rule in config.rules if rule["id"] == rule_id), None)


async def rule_path_problems(db, rule, schemas):
    filters = sample_filters(rule)
    if not filters:
        return []
    try:
        async with _read_only(db) as conn:
            cursor = await conn.execute("""
                WITH recent AS (
                    SELECT DISTINCT sample.id FROM unnest(%s::text[]) registry(id)
                    CROSS JOIN jsonb_to_recordset(%s) target(exclude boolean, patterns text[])
                    CROSS JOIN LATERAL (
                        SELECT e.id FROM events e
                        WHERE e.schema_id = registry.id AND e.status IN ('pending', 'processed')
                          AND e.received_at >= now() - interval '24 hours'
                          AND (cardinality(target.patterns) = 0 OR
                               (coalesce(e.schema_name, e.fields->>'$schema') LIKE ANY(target.patterns))
                               <> target.exclude)
                        ORDER BY e.received_at DESC, e.id DESC LIMIT 50
                    ) sample
                )
                SELECT e.schema_id, coalesce(e.schema_name, e.fields->>'$schema'), e.source_id, e.transport,
                       array_agg(DISTINCT path)
                FROM recent JOIN events e USING (id)
                CROSS JOIN LATERAL jsonb_object_keys(e.fields) AS path
                GROUP BY 1, 2, 3, 4
            """, ([schema["id"] for schema in schemas], Jsonb(filters)))
            samples = await cursor.fetchall()
    except OperationalError:
        logging.getLogger(__name__).warning("Recent rule-field samples unavailable", exc_info=True)
        return []
    return await run_in_threadpool(unknown_fields, rule, schemas, samples)


def _validate_shape(rule):
    route = rule.get("route", [])
    if not isinstance(route, list) or any(not isinstance(row, dict) for row in route):
        raise ValueError("route must be a list of destinations")
    for row in route:
        if not isinstance(row.get("to"), str) or not row["to"]:
            raise ValueError("each route needs a destination")
        channels = row.get("channels", "preferences")
        if channels != "preferences" and (
            not isinstance(channels, list)
            or any(not isinstance(channel, str) or channel not in Channel for channel in channels)
        ):
            raise ValueError("channels must be preferences or email, sms, phone and ui destinations")
    for key in ("group", "present", "throttle"):
        if key in rule and not isinstance(rule[key], dict):
            raise ValueError(f"{key} must be an object")
    if "owner" in rule and not isinstance(rule["owner"], str):
        raise ValueError("owner must be a string")


@router.post("/rules/draft")
async def suggest_rule(body: TextInput, x_user: str | None = Header(None), db=Depends(pool)):
    async with _read_only(db) as conn:
        config = await _config(conn)
    return await draft_rule(body.text, config.directory, actor=x_user)


def _test_rule(rule, sample, config, clock):
    rule = {"id": "draft", **rule, "enabled": True, "shadow": False}
    compile_match(rule["match"])
    if any(field.startswith("$") for field in sample):
        fields = flatten(sample)
        label = fields.get("$schema")
        schema = next((s for s in config.schemas if label in (s.get("id"), s.get("name"))), None)
        if schema is None:
            schema = detect_schema(fields, config.schemas) or {}
        severity = fields.get("$severity")
        event = CanonicalEvent(
            source_id="preview", transport="http", payload_hash="", raw={}, fields=fields,
            schema_id=schema.get("id"), received_at=clock.now(),
            severity=severity if isinstance(severity, str) and severity in Severity else Severity.unknown,
        )
    else:
        event = normalize(sample, {"id": "preview", "transport": "http"}, config.schemas, received_at=clock.now())
    evaluation = evaluate(event, Ruleset([rule], config.schemas), {}, clock)
    intents = []
    decision = evaluation.decisions[0]
    for op in evaluation.alert_ops:
        transition = Transition.digest_due if rule.get("group", {}).get("mode") == "digest" else op.routing_transition
        if op.notify or transition == Transition.digest_due:
            snapshot = alert_snapshot(op, event, config.directory, id=1)
            intents.extend(plan(snapshot, transition, rule, event.fields, config.directory, config.runbooks, {}, clock).intents)
    return {
        "matched": decision["reason"].get("matched", False), "trace": decision["reason"].get("trace", []),
        "decisions": evaluation.decisions, "recipients": sorted({intent.recipient for intent in intents}),
        "channels": sorted({intent.channel.value for intent in intents}),
    }


@router.post("/rules/test")
async def test_sample(body: TestRequest, db=Depends(pool)):
    async with _read_only(db) as conn:
        config = await _config(conn)
    try:
        _validate_shape(body.rule)
        return await run_in_threadpool(_test_rule, body.rule, body.event, config, FixedClock(SYSTEM.now()))
    except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/rules/check")
async def check_rule(body: CheckRequest, db=Depends(pool)):
    async with _read_only(db) as conn:
        config = await _config(conn)
    rule = body.rule if body.rule is not None else _rule_from(config, body.rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    try:
        _validate_shape(rule)
        result = await run_in_threadpool(check, rule, Ruleset(config.rules, config.schemas), config.schemas, config.directory)
        problems = await rule_path_problems(db, rule, config.schemas)
        return {**result, "warnings": [*result["warnings"], *problems], "problems": problems}
    except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/rules/{rule_id}/preview")
async def preview_rule(rule_id: str, body: PreviewRequest, db=Depends(pool)):
    if body.draft.get("id", rule_id) != rule_id:
        raise HTTPException(422, "Draft ID must match the rule in the request URL")
    async with _read_only(db) as conn, conn.cursor(row_factory=dict_row) as cursor:
        config = await _config(conn)
        current = _rule_from(config, rule_id)
        draft = {**(current or {}), **body.draft, "id": rule_id}
        try:
            _validate_shape(draft)
            compile_match(draft["match"])
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise HTTPException(422, str(exc)) from exc
        await cursor.execute("""
            SELECT id, source_id, transport, payload_hash, schema_id, severity, received_at, fields, trace, status
            FROM events WHERE status IN ('pending', 'processed')
            ORDER BY received_at DESC, id DESC LIMIT %s
        """, (body.last_n,))
        events = [CanonicalEvent.model_validate({**row, "raw": {}}) for row in await cursor.fetchall()]
    try:
        return await run_in_threadpool(preview_history, events, current, draft, config.schemas,
                                       config.directory, config.runbooks)
    except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError) as exc:
        raise HTTPException(422, str(exc)) from exc
