from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.rows import dict_row

from alerting.clock import SYSTEM
from alerting.rules.storage import Settings, alert_changed, audit, get_db

router = APIRouter(prefix="/v1", tags=["alerts"])


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


@router.get("/alerts")
async def alerts(team: str | None = None, status: str | None = None, q: str | None = None, db=Depends(get_db)):
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
        await cur.execute("select * from alerts"+where+""" order by case status when 'open' then 0
            when 'acknowledged' then 1 else 2 end,priority nulls last,score desc nulls last,last_seen_at desc,id""", params)
        items = await cur.fetchall()
        return {"items": items, "total": len(items)}


@router.get("/alerts/{id}")
async def detail(id: int, db=Depends(get_db)):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        alert = await get_alert(cur,id)
        await cur.execute("""select o.event_id,o.seen_at,e.fields from alert_occurrences o join events e on e.id=o.event_id
            where o.alert_id=%s order by event_id""", (id,))
        alert["occurrences"] = await cur.fetchall()
        await cur.execute("""select created_at ts,'decision' kind,concat(rule_id,' → ',outcome) as text from decisions where alert_id=%s
            union all select coalesce(delivered_at,created_at),'notification',concat(channel,' → ',recipient,': ',state)
                from notifications where alert_id=%s
            union all select d.finished_at,'notification',concat(n.channel,' → ',n.recipient,': ',d.outcome,' (attempt ',d.attempt,')')
                from delivery_attempts d join notifications n on n.id=d.notification_id where n.alert_id=%s and d.finished_at is not null
            union all select at,'audit',concat(actor,' ',action) from audit_log where target_kind='alert' and target_id=%s
            order by ts""", (id,id,id,str(id)))
        alert["timeline"] = await cur.fetchall()
        return alert


async def change(db, id, actor, resolve):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        alert = await get_alert(cur, id, lock=True)
        require_member(alert, actor, Settings.load())
        target = "resolved" if resolve else "acknowledged"
        if alert["status"] == target:
            return alert
        if alert["status"] == "resolved":
            raise HTTPException(409, "Alert already resolved")
        assignment = "resolved_by=%s,resolved_at=%s,resolve_reason='manual'" if resolve else "acked_by=%s,acked_at=%s"
        await cur.execute("update alerts set status=%s,"+assignment+",updated_at=now() where id=%s returning *",
                          (target,actor,SYSTEM.now(),id))
        row = await cur.fetchone()
        await audit(cur, actor, "alert.resolve" if resolve else "alert.ack", "alert", id)
        await alert_changed(cur,row)
        return row


@router.post("/alerts/{id}/ack")
async def ack(id: int, x_user: str = Header(...), db=Depends(get_db)):
    return await change(db,id,x_user,False)


@router.post("/alerts/{id}/resolve")
async def resolve(id: int, x_user: str = Header(...), db=Depends(get_db)):
    return await change(db,id,x_user,True)
