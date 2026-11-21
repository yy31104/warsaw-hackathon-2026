import re
import os
from secrets import compare_digest
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from alerting.clock import SYSTEM
from alerting.rules.storage import audit, get_db, get_settings
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


def approver_scope(directory, channel, sender):
    users = [u["id"] for u in directory["users"] if u.get("contacts", {}).get(channel) == sender]
    if len(users) != 1:
        return None, []
    actor = users[0]
    return actor, [t["id"] for t in directory["teams"] if actor in t.get("approvers", ())]


@router.post("/inbound/{channel}")
async def inbound(
    channel: Literal["sms"],
    body: Reply,
    x_provider_secret: str | None = Header(None),
    db=Depends(get_db),
):
    secret = os.getenv("INBOUND_SECRET")
    if (
        not secret
        or not x_provider_secret
        or not compare_digest(secret.encode(), x_provider_secret.encode())
    ):
        raise HTTPException(401, "Invalid provider secret")
    cfg = await get_settings(db)
    error = None
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("select pg_advisory_xact_lock(hashtextextended(%s, 1))", (body.sender,))
        now = SYSTEM.now()
        await cur.execute(
            "delete from approval_failures where sender=%s and failed_at<=%s",
            (body.sender, now - timedelta(minutes=15)),
        )
        await cur.execute(
            "select count(*) as failures from approval_failures where sender=%s", (body.sender,)
        )
        failures = (await cur.fetchone())["failures"]
        if failures >= 5:
            raise HTTPException(429, "Approval attempt limit reached")
        try:
            async with conn.transaction():
                actor, approve, result = await consume_reply(cur, channel, body, cfg)
        except HTTPException as exc:
            error = exc
            await cur.execute(
                "insert into approval_failures(sender,failed_at) values(%s,%s)", (body.sender, now)
            )
            if failures == 4:
                _, teams = approver_scope(cfg.directory, channel, body.sender)
                await cur.execute(
                    """select distinct r.alert_id from action_runs r join alerts a on a.id=r.alert_id
                       where a.team_id=any(%s) and r.state='pending_approval'
                       and r.approval_code is not null and r.approval_expires_at>%s order by r.alert_id""",
                    (teams, now),
                )
                affected = [row["alert_id"] for row in await cur.fetchall()]
                await audit(
                    cur,
                    "provider",
                    "approval.locked",
                    "sender",
                    body.sender,
                    {"window_minutes": 15, "alert_ids": affected},
                )
                error = HTTPException(429, "Approval attempt limit reached")
    if error:
        raise error
    return await execute(db, result["id"], actor, cfg) if approve else result


async def consume_reply(cur, channel, body, cfg):
    match = re.fullmatch(r"(YES|NO)\s+([0-9]{6})", body.body.strip(), re.IGNORECASE)
    if not match:
        raise HTTPException(422, "Use YES or NO followed by the six-digit code")
    actor, teams = approver_scope(cfg.directory, channel, body.sender)
    if actor is None:
        raise HTTPException(403, "Unrecognized sender")
    approve = match[1].upper() == "YES"
    if not teams:
        raise HTTPException(403, "Team approver required")
    code = match[2]
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
    return actor, approve, result
