from fastapi import APIRouter, Depends
from psycopg.rows import dict_row
from alerting.rules.storage import get_db

router = APIRouter(prefix="/v1", tags=["notifications"])


async def items(db, condition="", params=()):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("""select n.id,n.alert_id,n.kind,n.recipient,n.channel,n.state,n.attempts,n.fallback_of,
            coalesce((select jsonb_agg(jsonb_build_object('attempt',d.attempt,'outcome',d.outcome,
                'latency_ms',d.latency_ms,'error',d.error) order by d.attempt)
                from delivery_attempts d where d.notification_id=n.id),'[]') as attempt_log
            from notifications n """+condition+" order by n.created_at,n.id", params)
        return {"items": await cur.fetchall()}


@router.get("/notifications")
async def notifications(state: str | None = None, db=Depends(get_db)):
    return await items(db, "where n.state=%s", (state,)) if state else await items(db)


@router.get("/alerts/{id}/notifications")
async def alert_notifications(id: int, db=Depends(get_db)):
    return await items(db, "where n.alert_id=%s", (id,))
