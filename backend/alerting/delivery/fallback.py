"""Move a critical delivery to the next reachable preference in one transaction."""

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.delivery.fair import next_notification
from alerting.delivery.guard import can_dispatch
from alerting.routing.directory import by_id, preferences
from alerting.rules.storage import emit, get_settings


async def insert_replacement(cur, row, channel, contact, primary, now):
    ui = channel == "ui"
    rendered = dict(row["rendered"])
    rendered["meta"] = {**rendered.get("meta", {}), "fallback_from": row["channel"]}
    key = f"{row['alert_id']}:{row['kind']}:{row['recipient']}:{channel}:fallback-{row['id']}"
    state = "delivered" if ui else "pending"
    await cur.execute(
        """insert into notifications(alert_id,kind,recipient,channel,contact,rendered,priority,
        dedup_key,fallback_of,state,delivered_at,attempts,merged_into)
        values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
        (row["alert_id"], row["kind"], row["recipient"], channel, contact, Jsonb(rendered),
         row["priority"], key, row["id"], state, now if ui else None, int(ui), primary),
    )
    replacement = (await cur.fetchone())["id"]
    await emit(cur, "notifications", "notification.updated", id=replacement, alert_id=row["alert_id"],
               team_id=row["team_id"], state=state, attempts=int(ui))
    return replacement


async def fallback_one(db, channels, clock):
    directory = (await get_settings(db)).directory
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = await next_notification(cur, channels, clock.now(), fallback=True)
        if not row or not await can_dispatch(cur, row):
            return False

        members = [row]
        if row["kind"] == "merged":
            await cur.execute(
                """select n.*,a.team_id,a.severity from notifications n join alerts a on a.id=n.alert_id
                where n.merged_into=%s order by n.alert_id,n.id""", (row["id"],),
            )
            members.extend(await cur.fetchall())

        user = by_id(directory["users"]).get(row["recipient"], {})
        teams = by_id(directory["teams"])
        defaults = [channel for pref in directory.get("default_preferences", []) for channel in pref["channels"]]
        alternatives = []
        for member in members:
            team = teams.get(member["team_id"], {})
            preferred = preferences(user, team, directory, member["severity"], clock)
            alternatives.append([*preferred, *defaults, "ui"])
        await cur.execute(
            """select channel from notifications where alert_id=any(%s) and kind=%s and recipient=%s
            union select channel from delivery_breakers where opened_at is not null""",
            ([member["alert_id"] for member in members], row["kind"], row["recipient"]),
        )
        used = {r["channel"] for r in await cur.fetchall()}
        for channel in dict.fromkeys(alternatives[0]):
            contact = user.get("contacts", {}).get(channel)
            if channel not in used and (contact or channel == "ui") and all(channel in options for options in alternatives):
                break
        else:
            return False
        now = clock.now()
        primary = await insert_replacement(cur, row, channel, contact, None, now)
        for member in members[1:]:
            await insert_replacement(cur, member, channel, contact, primary, now)
        await cur.execute(
            "update notifications set state='fallback',locked_by=null,locked_until=null where id=%s",
            (row["id"],),
        )
        await emit(cur, "notifications", "notification.updated", id=row["id"], alert_id=row["alert_id"],
                   team_id=row["team_id"], state="fallback", attempts=row["attempts"])
        return True
