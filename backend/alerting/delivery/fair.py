"""Round-robin between due teams within a priority and a sender's channel set."""

import json


DELIVERIES = """from notifications n join alerts a on a.id=n.alert_id
    left join delivery_breakers b on b.channel=n.channel"""

TEAM_TURNS = """left join delivery_team_turns t on t.channels=%(channels)s and t.priority=n.priority
    and t.fallback=%(fallback)s and t.team_id=coalesce(a.team_id,'')"""

CHANNELS = """where n.channel=any(%(channels)s) and n.merged_into is null"""

DUE = """and (b.probe_after is null or b.probe_after<=%(now)s)
    and ((n.state in ('pending','retrying','unknown') and n.next_attempt_at<=%(now)s)
      or (n.state='sending' and n.locked_until<=%(now)s))"""

FALLBACK = """and b.opened_at < %(now)s - interval '30 seconds'
    and (a.severity='critical' or (n.kind='merged' and exists (
        select 1 from notifications member join alerts linked on linked.id=member.alert_id
        where member.merged_into=n.id and linked.severity='critical')))
    and n.kind in ('initial','update','digest','escalation','merged') and n.state in ('pending','retrying','unknown')"""


async def next_notifications(cur, channels, now, *, limit, fallback=False):
    """Lock up to limit messages from one team's turn at the highest due priority."""
    if limit < 1:
        raise ValueError("delivery batch limit must be positive")
    channels = sorted(set(channels))
    if not channels:
        return []
    args = {"channels": channels, "now": now, "fallback": fallback, "limit": limit}
    conditions = f"{CHANNELS} {FALLBACK if fallback else DUE}"
    teams = f"{DELIVERIES} {TEAM_TURNS} {conditions}"
    await cur.execute(
        f"""select priority,team,last_turn from (
            select n.priority,coalesce(a.team_id,'') as team,t.last_turn,min(n.next_attempt_at) as oldest,
                   min(n.priority) over () as highest
            {teams} group by n.priority,coalesce(a.team_id,''),t.last_turn
        ) candidates where %(fallback)s or priority=highest
        order by priority,last_turn nulls first,oldest,team""",
        args,
    )
    for team in await cur.fetchall():
        args["lock"] = json.dumps(["delivery-team", channels, fallback, team["priority"], team["team"]])
        args.update(team)
        # Recheck both the turn and priority on the locking snapshot: another worker
        # may have advanced this team, or a critical message may have just committed.
        await cur.execute(
            f"""select n.*,a.team_id,a.severity {teams}
            and n.priority=%(priority)s and coalesce(a.team_id,'')=%(team)s
            and t.last_turn is not distinct from %(last_turn)s
            and (select pg_try_advisory_xact_lock(hashtextextended(%(lock)s,0)))
            and (%(fallback)s or not exists (
                select 1 {DELIVERIES} {conditions} and n.priority<%(priority)s
            ))
            order by n.next_attempt_at,n.id limit %(limit)s for update of n skip locked""",
            args,
        )
        rows = await cur.fetchall()
        if rows:
            # A former holder can commit between the row query's snapshot and its
            # advisory-lock test. Advance only the turn that discovery actually saw.
            await cur.execute(
                """insert into delivery_team_turns as t(channels,fallback,priority,team_id)
                values(%(channels)s,%(fallback)s,%(priority)s,%(team)s)
                on conflict (channels,fallback,priority,team_id) do update set last_turn=excluded.last_turn
                where t.last_turn is not distinct from %(last_turn)s returning last_turn""",
                args,
            )
            if await cur.fetchone():
                return rows
    return []


async def next_notification(cur, channels, now, *, fallback=False):
    rows = await next_notifications(cur, channels, now, limit=1, fallback=fallback)
    return rows[0] if rows else None
