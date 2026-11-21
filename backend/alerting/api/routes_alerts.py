from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM
from alerting.contracts import Outcome
from alerting.db import pool
from alerting.routing.directory import by_id
from alerting.rules.silences import SILENCE_COLUMNS, SilenceDraft, active_rows
from alerting.rules.storage import alert_changed, audit, close_settings, get_db, get_settings
from alerting.runbooks.approvals import pending_count, pending_for_alert


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await close_settings(pool())


router = APIRouter(prefix="/v1", tags=["alerts"], lifespan=lifespan)


# The feed, detail and mutation responses expose the same alert fields as the frontend fixture.
ALERT_FIELDS = (
    "id rule_id rule_version alert_key status severity priority score score_breakdown lane team_id "
    "title body color fields group_values runbook_id occurrence_count first_seen_at last_seen_at "
    "acked_by acked_at resolved_by resolved_at"
).split()


def public_alert(row):
    return {key: row[key] for key in ALERT_FIELDS}


async def get_alert(cur, id, lock=False):
    await cur.execute("select * from alerts where id=%s" + (" for update" if lock else ""), (id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Alert not found")
    return row


def require_member(alert, actor, settings):
    team = next((t for t in settings.directory["teams"] if t["id"] == alert["team_id"]), {})
    if actor not in team.get("members", []):
        raise HTTPException(403, "Team membership required")
    return team


@router.get("/silences")
async def silences(team: str | None = None, db=Depends(get_db)):
    async with db.connection() as conn:
        return {"items": await active_rows(conn, SYSTEM.now(), team=team)}


@router.post("/silences", status_code=201)
async def create_silence(draft: SilenceDraft, x_user: str = Header(...), db=Depends(get_db)):
    now = SYSTEM.now()
    if draft.expires_at <= now:
        raise HTTPException(422, "Silence expiry must be in the future")
    require_member({"team_id": draft.team_id}, x_user, await get_settings(db))
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""insert into silences (team_id,matcher,include_critical,created_by,created_at,expires_at)
                values (%s,%s,%s,%s,%s,%s) returning {SILENCE_COLUMNS}""",
            (draft.team_id, Jsonb(draft.match), draft.include_critical, x_user, now, draft.expires_at),
        )
        row = await cur.fetchone()
        await audit(cur, x_user, "silence.create", "silence", row["id"], {
            "team_id": draft.team_id,
            "expires_at": draft.expires_at.isoformat(),
            "include_critical": draft.include_critical,
        })
        return row


@router.delete("/silences/{id}", status_code=204)
async def delete_silence(id: int, x_user: str = Header(...), db=Depends(get_db)):
    settings = await get_settings(db)
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("select team_id,revoked_at from silences where id=%s for update", (id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "Silence not found")
        require_member(row, x_user, settings)
        if row["revoked_at"] is None:
            await cur.execute("update silences set revoked_at=%s where id=%s", (SYSTEM.now(), id))
            await audit(cur, x_user, "silence.delete", "silence", id, {"team_id": row["team_id"]})
    return Response(status_code=204)


@router.get("/alerts")
async def alerts(
    team: str | None = None, status: str | None = None, q: str | None = None, db=Depends(get_db)
):
    conditions, params = [], []
    for column, value in (("team_id", team), ("status", status)):
        if value:
            conditions.append(f"{column}=%s")
            params.append(value)
    if q:
        conditions.append("concat_ws(' ',title,body,alert_key,rule_id) ilike %s")
        params.append(f"%{q}%")
    where = " where " + " and ".join(conditions) if conditions else ""
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select * from alerts"
            + where
            + """ order by case status when 'open' then 0
            when 'acknowledged' then 1 else 2 end,priority nulls last,score desc nulls last,last_seen_at desc,id""",
            params,
        )
        items = await cur.fetchall()
        return {"items": [public_alert(row) for row in items], "total": len(items)}


@router.get("/alerts/{id}")
async def detail(id: int, db=Depends(get_db)):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        alert = public_alert(await get_alert(cur, id))
        pending = await pending_for_alert(cur, id)
        if pending:
            settings = await get_settings(db, conn=conn)
            team = by_id(settings.directory["teams"]).get(alert["team_id"], {})
            for run in pending:
                run["approvers"] = team.get("approvers", [])
        alert["pending_actions"] = pending
        await cur.execute(
            """select o.event_id,o.seen_at,e.fields from alert_occurrences o join events e on e.id=o.event_id
            where o.alert_id=%s order by event_id""",
            (id,),
        )
        alert["occurrences"] = await cur.fetchall()
        await cur.execute(
            """select created_at ts,'decision' kind,concat(rule_id,' → ',outcome) as text from decisions where alert_id=%s
            union all select coalesce(delivered_at,created_at),'notification',concat(channel,' → ',recipient,': ',state)
                from notifications where alert_id=%s
            union all select d.finished_at,'notification',concat(n.channel,' → ',n.recipient,': ',d.outcome,' (attempt ',d.attempt,')')
                from delivery_attempts d join notifications n on n.id=d.notification_id where n.alert_id=%s and d.finished_at is not null
            union all select at,'audit',concat(actor,' ',action) from audit_log
                where (target_kind='alert' and target_id=%s)
                   or (target_kind='sender' and action='approval.locked'
                       and detail->'alert_ids' @> jsonb_build_array(%s::bigint))
            order by ts""",
            (id, id, id, str(id), id),
        )
        alert["timeline"] = await cur.fetchall()
        return alert


@router.get("/stats/pending-approvals")
async def pending_approvals(team: str | None = None, db=Depends(get_db)):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return await pending_count(cur, team)


async def change(db, id, actor, resolve):
    settings = await get_settings(db)
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        alert = await get_alert(cur, id, lock=True)
        require_member(alert, actor, settings)
        target = "resolved" if resolve else "acknowledged"
        if alert["status"] == target:
            return public_alert(alert)
        if alert["status"] == "resolved":
            raise HTTPException(409, "Alert already resolved")
        assignment = (
            "resolved_by=%s,resolved_at=%s,resolve_reason='manual'"
            if resolve
            else "acked_by=%s,acked_at=%s"
        )
        await cur.execute(
            "update alerts set status=%s,"
            + assignment
            + ",updated_at=now() where id=%s returning *",
            (target, actor, SYSTEM.now(), id),
        )
        row = await cur.fetchone()
        await audit(cur, actor, "alert.resolve" if resolve else "alert.ack", "alert", id)
        await alert_changed(cur, row)
        return public_alert(row)


@router.post("/alerts/{id}/ack")
async def ack(id: int, x_user: str = Header(...), db=Depends(get_db)):
    return await change(db, id, x_user, False)


@router.post("/alerts/{id}/resolve")
async def resolve(id: int, x_user: str = Header(...), db=Depends(get_db)):
    return await change(db, id, x_user, True)


@router.get("/events/{id}/explain")
async def explain(id: int, db=Depends(get_db)):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select id,schema_id,schema_name,schema_version,trace,status from events where id=%s",
            (id,),
        )
        event = await cur.fetchone()
        if not event:
            raise HTTPException(404, "Event not found")
        await cur.execute(
            "select rule_id,rule_version,outcome,reason,alert_id from decisions where event_id=%s order by id",
            (id,),
        )
        decisions = await cur.fetchall()
        live = [
            decision
            for decision in decisions
            if decision["outcome"] != Outcome.SHADOW_MATCH
            and not (decision["reason"] or {}).get("shadow")
        ]
        outcome = next((d["outcome"] for d in live if d["outcome"] != "NO_MATCH"), None)
        keys = [key for d in live for key in (d["reason"] or {}).get("notifications", [])]
        buckets = [
            key for d in live for key in (d["reason"] or {}).get("throttle_buckets", [])
        ]
        await cur.execute(
            """select id,alert_id,kind,channel,recipient,state from notifications
            where dedup_key=any(%s) or throttle_bucket=any(%s)""",
            (keys, buckets),
        )
        return {
            **event,
            "schema_detection": event["trace"].get(
                "schema_detection", {"schema_id": event["schema_id"]}
            ),
            "decisions": decisions,
            "notifications": await cur.fetchall(),
            "outcome": outcome or ("NO_MATCH" if live else event["status"].upper()),
        }
