import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

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
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select id from action_runs where state='pending_approval' and approval_code=%s",
            (match[2],),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "No pending approval")
        result = await decide(cur, row["id"], actor, approve, cfg, match[2])
    return await execute(db, row["id"], actor, cfg) if approve else result
