from psycopg.rows import dict_row

from alerting.rules.storage import emit


def requires_open_alert(row):
    return row["kind"] == "ladder_step" or row["rendered"].get("meta", {}).get("requires_open_alert")


async def can_dispatch(cur, row, *, cancelled_at=None):
    if not requires_open_alert(row):
        return True
    # Order the final decision against a concurrent ack without holding either lock during I/O.
    await cur.execute("select status from alerts where id=%s for share skip locked", (row["alert_id"],))
    alert = await cur.fetchone()
    if alert is None:
        return False
    if alert["status"] == "open":
        return True
    await cur.execute(
        "update notifications set state='cancelled',locked_by=null,locked_until=null where id=%s",
        (row["id"],),
    )
    if cancelled_at is not None:
        await cur.execute(
            """update delivery_attempts set finished_at=%s,error='alert closed before dispatch'
               where notification_id=%s and attempt=%s and finished_at is null""",
            (cancelled_at, row["id"], row["attempts"]),
        )
    await emit(cur, "notifications", "notification.updated", id=row["id"], alert_id=row["alert_id"],
               team_id=row["team_id"], state="cancelled", attempts=row["attempts"])
    return False


async def recheck_dispatch(db, row, clock):
    if not requires_open_alert(row):
        return True
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        now = clock.now()
        await cur.execute(
            """select id from notifications where id=%s and locked_by=%s
               and locked_until>%s for update skip locked""",
            (row["id"], row["locked_by"], now),
        )
        if not await cur.fetchone():
            return False
        return await can_dispatch(cur, row, cancelled_at=now)
