import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from alerting.clock import SYSTEM
from alerting.rules.storage import get_db, get_settings
from alerting.runbooks.approvals import decide, execute

router = APIRouter(prefix="/v1", tags=["notifications"])


async def items(db, condition="", params=()):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select n.id,n.alert_id,n.kind,n.recipient,n.channel,n.state,n.attempts,n.fallback_of,
            coalesce((select jsonb_agg(jsonb_build_object('attempt',d.attempt,'outcome',d.outcome,
                'latency_ms',d.latency_ms,'error',d.error) order by d.attempt)
                from delivery_attempts d where d.notification_id=n.id),'[]') as attempt_log
            from notifications n """
            + condition
            + " order by n.created_at,n.id",
            params,
        )
        return {"items": await cur.fetchall()}


@router.get("/notifications")
async def notifications(state: str | None = None, db=Depends(get_db)):
    return await items(db, "where n.state=%s", (state,)) if state else await items(db)


@router.get("/alerts/{id}/notifications")
async def alert_notifications(id: int, db=Depends(get_db)):
    return await items(db, "where n.alert_id=%s", (id,))


class Reply(BaseModel):
    sender: str = Field(alias="from", max_length=128)
    body: str = Field(max_length=64)


@router.post("/inbound/{channel}")
async def inbound(channel: Literal["sms"], body: Reply, db=Depends(get_db)):
    match = re.fullmatch(r"(YES|NO)\s+(\d{4})", body.body.strip(), re.IGNORECASE)
    if not match:
        raise HTTPException(422, "Use YES or NO followed by the four-digit code")
    cfg = await get_settings(db)
    users = [
        u["id"] for u in cfg.directory["users"] if u.get("contacts", {}).get(channel) == body.sender
    ]
    if len(users) != 1:
        raise HTTPException(403, "Unrecognized sender")
    actor = users[0]
    approve = match[1].upper() == "YES"
    teams = [t["id"] for t in cfg.directory["teams"] if actor in t.get("approvers", ())]
    if not teams:
        raise HTTPException(403, "Team approver required")
    code = match[2]
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        scoped = """from action_runs r join alerts a on a.id=r.alert_id
                    where r.approval_code=%s and a.team_id=any(%s) and r.state='pending_approval'"""
        now = SYSTEM.now()
        await cur.execute(
            "select r.id "
            + scoped
            + " and r.approval_expires_at>%s order by r.id limit 2 for update of r",
            (code, teams, now),
        )
        rows = await cur.fetchall()
        if not rows:
            await cur.execute(
                "select 1 " + scoped + " and r.approval_expires_at<=%s limit 1", (code, teams, now)
            )
            if await cur.fetchone():
                raise HTTPException(409, "Approval expired")
            raise HTTPException(404, "No pending approval for this sender")
        if len(rows) > 1:
            raise HTTPException(409, "Ambiguous approval. Use the alert page.")
        run_id = rows[0]["id"]
        result = await decide(cur, run_id, actor, approve, cfg, code)
    return await execute(db, run_id, actor, cfg) if approve else result
