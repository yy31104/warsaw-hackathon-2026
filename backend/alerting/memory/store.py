"""Capture a team's resolution and read its historical hints in the caller's transaction."""

from fastapi import HTTPException

from alerting.clock import SYSTEM
from alerting.memory import ResolutionNote, incident_key
from alerting.rules.storage import alert_changed, audit


async def _alert_key(cur, alert):
    await cur.execute(
        """select service, schema_id, schema_name, schema_version,
                  jsonb_build_object('$service', fields->'$service', '$schema', fields->'$schema',
                                     '$error_code', fields->'$error_code') as fields
           from events where id=%s""",
        (alert["representative_event_id"],),
    )
    return incident_key(await cur.fetchone() or {"fields": alert["fields"]})


async def resolve_with_note(cur, alert, actor: str, note: ResolutionNote, settings):
    """Requires the alert's row lock. Retries preserve the first confirmed snapshot."""
    if actor == "system" or actor.startswith("llm-suggestion:"):
        raise HTTPException(403, "A human team member must confirm the resolution")
    alert_id = alert["id"]
    resolution = note.verified_resolution or note.note
    already_resolved = alert["status"] == "resolved"
    if already_resolved:
        if alert["resolve_reason"] != "manual" or alert["resolved_by"] != actor:
            raise HTTPException(
                409, "Only the operator who manually resolved this alert can confirm it"
            )
        await cur.execute(
            "select notes, verified_resolution from incident_memory where alert_id=%s", (alert_id,)
        )
        existing = await cur.fetchone()
        if existing:
            if existing["notes"] != note.note or existing["verified_resolution"] != resolution:
                raise HTTPException(409, "A different resolution has already been confirmed")
            return alert
    else:
        await cur.execute(
            """update alerts set status='resolved', resolve_reason='manual', resolved_by=%s,
                                 resolved_at=%s, updated_at=now() where id=%s returning *""",
            (actor, SYSTEM.now(), alert_id),
        )
        alert = await cur.fetchone()
        await audit(cur, actor, "alert.resolve", "alert", alert_id)
        await alert_changed(cur, alert)

    await cur.execute(
        """select id from audit_log where target_kind='alert' and target_id=%s
           and action='alert.resolve' and actor=%s order by id desc limit 1""",
        (str(alert_id), actor),
    )
    confirmed = await cur.fetchone()
    if not confirmed:
        raise HTTPException(409, "This alert has no audited manual resolution")

    # Older alerts may lack a pinned version. Use the registry revision at resolution time,
    # or the bootstrap version of a file-backed runbook seen during resolution. A later note
    # cannot establish which unversioned file was present when the operator closed the alert.
    file_version = None
    if not already_resolved:
        file_version = next(
            (
                book.get("version", 1)
                for book in settings.runbooks
                if book["id"] == alert["runbook_id"] and book.get("file")
            ),
            None,
        )
    params = {
        **await _alert_key(cur, alert),
        "alert_id": alert_id,
        "target_id": str(alert_id),
        "team_id": alert["team_id"],
        "runbook_id": alert["runbook_id"],
        "runbook_version": alert["runbook_version"],
        "file_version": file_version,
        "resolved_at": alert["resolved_at"],
        "confirmed_at": SYSTEM.now(),
        "confirmed_by": actor,
        "resolution_audit_id": confirmed["id"],
        "resolution": resolution,
        "notes": note.note,
    }
    await cur.execute(
        """insert into incident_memory(alert_id, team_id, service, error_code, schema_id, schema_name,
                   schema_version, runbook_id, runbook_version, actions_attempted, verified_resolution,
                   notes, confirmed_by, resolved_at, confirmed_at, resolution_audit_id)
           values (%(alert_id)s, %(team_id)s, %(service)s, %(error_code)s, %(schema_id)s, %(schema)s,
                   %(schema_version)s, %(runbook_id)s,
                   coalesce(%(runbook_version)s::int,
                       (select version from config_objects where kind='runbook' and id=%(runbook_id)s
                        and created_at <= %(resolved_at)s order by created_at desc, version desc limit 1),
                       %(file_version)s::int),
                   (select coalesce(jsonb_agg(jsonb_build_object('run_id', r.id, 'action', r.action,
                                   'ok', l.detail->'ok') order by l.id), '[]'::jsonb)
                    from audit_log l join action_runs r on r.id::text=l.detail->>'run_id'
                    where l.target_kind='alert' and l.target_id=%(target_id)s and r.alert_id=%(alert_id)s
                      and l.action='action.execute' and l.id < %(resolution_audit_id)s),
                   %(resolution)s, %(notes)s, %(confirmed_by)s, %(resolved_at)s, %(confirmed_at)s,
                   %(resolution_audit_id)s)""",
        params,
    )
    await audit(
        cur,
        actor,
        "incident_memory.confirm",
        "alert",
        alert_id,
        {"resolution_audit_id": confirmed["id"]},
    )
    return alert


async def similar(cur, alert, limit: int):
    params = {
        **await _alert_key(cur, alert),
        "team": alert["team_id"],
        "alert_id": alert["id"],
        "limit": limit,
    }
    await cur.execute(
        """select * from (
               select alert_id, service, error_code, schema_name as schema, schema_id, schema_version,
                      runbook_id, runbook_version, actions_attempted, verified_resolution, notes,
                      confirmed_by, resolved_at, confirmed_at,
                      case when service=%(service)s and error_code=%(error_code)s then 'service_error_code'
                           when schema_name=%(schema)s then 'schema' end as match_reason
               from incident_memory where team_id=%(team)s and alert_id<>%(alert_id)s
           ) as cards where match_reason is not null
           order by (match_reason='service_error_code') desc, resolved_at desc, alert_id desc limit %(limit)s""",
        params,
    )
    return await cur.fetchall()
