"""Move a critical delivery to the next reachable preference in one transaction."""

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.delivery.fair import next_notification
from alerting.routing.directory import by_id, preferences
from alerting.rules.storage import emit, get_settings


async def fallback_one(db, channels, clock):
    directory = (await get_settings(db)).directory
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = await next_notification(cur, channels, clock.now(), fallback=True)
        if not row:
            return False

        user = by_id(directory["users"]).get(row["recipient"], {})
        team = by_id(directory["teams"]).get(row["team_id"], {})
        options = list(preferences(user, team, directory, row["severity"], clock))
        for pref in directory.get("default_preferences", []):
            options.extend(pref["channels"])
        await cur.execute(
            """select channel from notifications where alert_id=%s and kind=%s and recipient=%s
            union select channel from delivery_breakers where opened_at is not null""",
            (row["alert_id"], row["kind"], row["recipient"]),
        )
        used = {r["channel"] for r in await cur.fetchall()}
        contact = None
        for channel in dict.fromkeys([*options, "ui"]):
            contact = user.get("contacts", {}).get(channel)
            if channel not in used and (contact or channel == "ui"):
                break
        else:
            return False
        ui = channel == "ui"
        rendered = dict(row["rendered"])
        rendered["meta"] = {**rendered.get("meta", {}), "fallback_from": row["channel"]}
        await cur.execute(
            """insert into notifications(alert_id,kind,recipient,channel,contact,rendered,priority,
            dedup_key,fallback_of,state,delivered_at,attempts) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (
                row["alert_id"],
                row["kind"],
                row["recipient"],
                channel,
                contact,
                Jsonb(rendered),
                row["priority"],
                f"{row['alert_id']}:{row['kind']}:{row['recipient']}:{channel}:fallback-{row['id']}",
                row["id"],
                "delivered" if ui else "pending",
                clock.now() if ui else None,
                int(ui),
            ),
        )
        replacement = await cur.fetchone()
        await cur.execute(
            "update notifications set state='fallback',locked_by=null,locked_until=null where id=%s",
            (row["id"],),
        )
        for id_, state, attempts in [
            (replacement["id"], "delivered" if ui else "pending", int(ui)),
            (row["id"], "fallback", row["attempts"]),
        ]:
            await emit(
                cur,
                "notifications",
                "notification.updated",
                id=id_,
                alert_id=row["alert_id"],
                team_id=row["team_id"],
                state=state,
                attempts=attempts,
            )
        return True
