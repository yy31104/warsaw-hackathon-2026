"""Rule versions and reviewed proposals (CONTRACT §9)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StringConstraints

from alerting.api.routes_rule_assist import router as assist_router
from alerting.config import loader
from alerting.config.access import actor_header, actor_teams, can_manage, config_write, require_actor, require_owner
from alerting.config.store import ConfigError, audit, get_object, require_current_version, write_object
from alerting.config.validation import RuleDraft, describe_rule
from alerting.db import pool
from alerting.routing.directory import team_for

router = APIRouter(prefix="/v1", tags=["rules"])
router.include_router(assist_router)


def public_rule(rule):
    return {"enabled": True, **rule, "plain_english": describe_rule(rule)}


@router.get("/rules")
async def list_rules(db=Depends(pool)):
    return {"items": [public_rule(rule) for rule in await loader.rules(db)]}


async def publish_rule(conn, draft, actor, directory, *, current):
    if current:
        if current.origin == "file":
            raise ConfigError(409, "This rule is managed in a configuration file")
        require_owner(actor, current.spec.get("owner"), directory)
    require_owner(actor, draft.owner, directory)
    team_id = team_for(draft.owner, directory)
    saved = await write_object(conn, "rule", draft.id, draft.spec(), current=current, origin="ui",
                               actor=actor, team_id=team_id, audit_source=draft.source)
    return public_rule(saved)


@router.post("/rules", status_code=201)
async def create_rule(draft: RuleDraft, actor=Depends(actor_header), db=Depends(pool)):
    async with config_write(db) as (conn, snapshot):
        current = await get_object(conn, "rule", draft.id)
        if current:
            reason = "This rule is managed in a configuration file" if current.origin == "file" else "Rule ID already exists"
            raise ConfigError(409, reason)
        return await publish_rule(conn, draft, actor, snapshot.directory, current=None)


@router.put("/rules/{id}")
async def update_rule(id: str, draft: RuleDraft, actor=Depends(actor_header), db=Depends(pool)):
    if draft.id != id:
        raise HTTPException(422, "Rule ID must match the request path")
    async with config_write(db) as (conn, snapshot):
        current = await get_object(conn, "rule", id)
        if current and current.origin == "file":
            raise ConfigError(409, "This rule is managed in a configuration file")
        if current is None or not current.active:
            raise ConfigError(404, "Rule not found")
        require_owner(actor, current.spec.get("owner"), snapshot.directory)
        require_current_version(current, draft.version)
        return await publish_rule(conn, draft, actor, snapshot.directory, current=current)


class ProposalInput(BaseModel):
    alert_id: int = Field(gt=0)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    draft: RuleDraft | None = None


class ProposalApproval(BaseModel):
    draft: RuleDraft | None = None


def public_proposal(row, rule):
    return {**row, "text": row["rationale"], "rule_owner": rule.get("owner") if rule else None}


@router.get("/proposals")
async def list_proposals(rule_id: str | None = None,
                         status: Literal["open", "approved", "rejected"] | None = None,
                         limit: int = Query(50, ge=1, le=200), actor=Depends(actor_header), db=Depends(pool)):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        snapshot = await loader.read_snapshot(conn)
        try:
            require_actor(actor, snapshot.directory)
        except ConfigError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        teams = actor_teams(actor, snapshot.directory)
        rules = {rule["id"]: rule for rule in snapshot.rules}
        owned = [key for key, rule in rules.items() if can_manage(actor, rule.get("owner"), snapshot.directory)]
        await cur.execute("""
            select * from rule_proposals
            where (%s::text is null or rule_id=%s) and (%s::text is null or status=%s)
            and (proposed_by=%s or team_id=any(%s) or (team_id is null and rule_id=any(%s)))
            order by id desc limit %s
        """, (rule_id, rule_id, status, status, actor, teams, owned, limit))
        return {"items": [public_proposal(row, rules.get(row["rule_id"])) for row in await cur.fetchall()]}


@router.post("/proposals", status_code=201)
async def create_proposal(body: ProposalInput, actor=Depends(actor_header), db=Depends(pool)):
    async with config_write(db) as (conn, snapshot), conn.cursor(row_factory=dict_row) as cur:
        require_actor(actor, snapshot.directory)
        await cur.execute("select rule_id,rule_version,team_id from alerts where id=%s", (body.alert_id,))
        alert = await cur.fetchone()
        if alert is None:
            raise HTTPException(404, "Alert not found")
        if alert["team_id"] not in actor_teams(actor, snapshot.directory):
            raise HTTPException(403, "The incident belongs to a different team")
        if body.draft and body.draft.id != alert["rule_id"]:
            raise HTTPException(422, "The proposed draft must use the incident's rule ID")
        await cur.execute("""
            insert into rule_proposals (alert_id,team_id,rule_id,base_version,draft,rationale,source,proposed_by)
            values (%s,%s,%s,%s,%s,%s,'human',%s) returning *
        """, (body.alert_id, alert["team_id"], alert["rule_id"], alert["rule_version"],
              Jsonb(body.draft.spec() if body.draft else {}), body.text, actor))
        row = await cur.fetchone()
        await audit(conn, actor, "proposal.create", "proposal", row["id"], {"alert_id": body.alert_id})
        rule = next((rule for rule in snapshot.rules if rule["id"] == alert["rule_id"]), None)
        return public_proposal(row, rule)


@router.post("/proposals/{id}/approve")
async def approve_proposal(id: int, body: ProposalApproval | None = None,
                           actor=Depends(actor_header), db=Depends(pool)):
    async with config_write(db) as (conn, snapshot), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("select * from rule_proposals where id=%s for update", (id,))
        proposal = await cur.fetchone()
        if proposal is None:
            raise HTTPException(404, "Proposal not found")
        current = await get_object(conn, "rule", proposal["rule_id"])
        if current is None or not current.active:
            raise HTTPException(409, "The proposal's rule is no longer active")
        require_owner(actor, current.spec.get("owner"), snapshot.directory)
        if proposal["team_id"] and proposal["team_id"] not in actor_teams(actor, snapshot.directory):
            raise HTTPException(403, "The proposal belongs to a different team")
        if proposal["status"] == "approved":
            return public_proposal(proposal, current.spec)
        if proposal["status"] != "open" or current.origin == "file":
            raise HTTPException(409, "The proposal cannot modify the current rule")
        require_current_version(current, proposal["base_version"])
        draft = body.draft if body else None
        if draft is None and proposal["draft"]:
            draft = RuleDraft.model_validate(proposal["draft"])
        if draft is None or draft.id != proposal["rule_id"]:
            raise HTTPException(422, "Review a draft for this rule before approving")
        require_current_version(current, draft.version)
        saved = await publish_rule(conn, draft, actor, snapshot.directory, current=current)
        await cur.execute("""
            update rule_proposals set status='approved',decided_by=%s,decided_at=now(),draft=%s,
            approved_version=%s where id=%s returning *
        """, (actor, Jsonb(draft.spec()), saved["version"], id))
        result = await cur.fetchone()
        await audit(conn, actor, "proposal.approve", "proposal", id, {"version": saved["version"]})
        return {**public_proposal(result, saved), "saved_rule": saved}
