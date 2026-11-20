"""Round-robin between due teams within a priority and a sender's channel set."""

import json


TEAMS = """from notifications n join alerts a on a.id=n.alert_id
    left join delivery_breakers b on b.channel=n.channel
    left join delivery_team_turns t on t.channels=%(channels)s and t.priority=n.priority
        and t.fallback=%(fallback)s and t.team_id=coalesce(a.team_id,'')
    where n.channel=any(%(channels)s) and n.merged_into is null"""

DUE = """and (b.probe_after is null or b.probe_after<=%(now)s)
    and ((n.state in ('pending','retrying','unknown') and n.next_attempt_at<=%(now)s)
      or (n.state='sending' and n.locked_until<=%(now)s))"""

FALLBACK = """and b.opened_at < %(now)s - interval '30 seconds'
    and (a.severity='critical' or (n.kind='merged' and exists (
        select 1 from notifications member join alerts linked on linked.id=member.alert_id
        where member.merged_into=n.id and linked.severity='critical')))
    and n.kind in ('initial','update','digest','escalation','merged') and n.state in ('pending','retrying','unknown')"""


async def next_notification(cur, channels, now, *, fallback=False):
    channels = sorted(set(channels))
    args = {"channels": channels, "now": now, "fallback": fallback}
    eligible = f"{TEAMS} {FALLBACK if fallback else DUE}"
    await cur.execute(
        f"""select n.priority,coalesce(a.team_id,'') as team,t.last_turn {eligible}
        group by n.priority,coalesce(a.team_id,''),t.last_turn
        order by n.priority,t.last_turn nulls first,min(n.next_attempt_at),coalesce(a.team_id,'')""",
        args,
    )
    for team in await cur.fetchall():
        # A second worker takes another team immediately while this worker reserves a row.
        lock = json.dumps(["delivery-team", channels, fallback, team["priority"], team["team"]])
        await cur.execute("select pg_try_advisory_xact_lock(hashtextextended(%s,0)) as acquired", (lock,))
        if not (await cur.fetchone())["acquired"]:
            continue
        args.update(team)
        # Another worker may have finished this team's claim since the first query.
        await cur.execute(
            f"""select n.*,a.team_id,a.severity {eligible} and n.priority=%(priority)s
            and coalesce(a.team_id,'')=%(team)s and t.last_turn is not distinct from %(last_turn)s
            order by n.next_attempt_at,n.id limit 1 for update of n skip locked""",
            args,
        )
        row = await cur.fetchone()
        if row:
            await cur.execute(
                """insert into delivery_team_turns(channels,fallback,priority,team_id)
                values(%(channels)s,%(fallback)s,%(priority)s,%(team)s)
                on conflict (channels,fallback,priority,team_id) do update set last_turn=excluded.last_turn""",
                args,
            )
            return row
    return None
