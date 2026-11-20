from alerting.rules.storage import emit


async def can_dispatch(cur, row):
    if row["kind"] != "ladder_step" and not row["rendered"].get("meta", {}).get("requires_open_alert"):
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
    await emit(cur, "notifications", "notification.updated", id=row["id"], alert_id=row["alert_id"],
               team_id=row["team_id"], state="cancelled", attempts=row["attempts"])
    return False
