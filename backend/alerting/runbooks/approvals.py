"""Lifecycle of a run. Provider I/O happens strictly between the claim and completion transactions."""

from datetime import timedelta
from secrets import randbelow

import httpx
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM
from alerting.contracts import (
    ActionContext,
    ActionResult,
    NotificationIntent,
    RenderedMessage,
)
from alerting.routing import plan
from alerting.routing.directory import by_id
from alerting.rules.storage import alert_changed, audit, emit, enqueue
from alerting.runbooks.actions import REGISTRY
from alerting.runbooks.render import render


def public(row):
    return {k: v for k, v in row.items() if k != "approval_code"}


async def changed(cur, row, team):
    await emit(
        cur,
        "actions",
        "action.updated",
        id=row["id"],
        alert_id=row["alert_id"],
        team_id=team,
        state=row["state"],
        approved_by=row.get("approved_by"),
    )


async def runbook_for(cur, alert, settings):
    book = next((b for b in settings.runbooks if b["id"] == alert["runbook_id"]), None)
    if not book:
        raise HTTPException(404, "No runbook attached")
    await cur.execute("select fields from events where id=%s", (alert["representative_event_id"],))
    event = await cur.fetchone()
    await cur.execute(
        "select step_index,action,state from action_runs where alert_id=%s and runbook_id=%s order by id",
        (alert["id"], book["id"]),
    )
    team = by_id(settings.directory["teams"]).get(alert["team_id"], {})
    return render(
        book,
        event["fields"] if event else alert["fields"],
        await cur.fetchall(),
        team.get("approvers", []),
    )


async def request_run(cur, alert, step, actor, settings):
    if not step.get("action") or step["action"] not in REGISTRY:
        raise HTTPException(422, "This step requires manual execution")
    required = step["approval"] == "required"
    team = by_id(settings.directory["teams"])[alert["team_id"]]
    users = by_id(settings.directory["users"])
    contacts = [
        (u, users.get(u, {}).get("contacts", {}).get("sms")) for u in team.get("approvers", [])
    ]
    if required and not any(c for _, c in contacts):
        raise HTTPException(422, "No approver has an SMS contact")
    now = SYSTEM.now()
    if required:
        await cur.execute(
            """update action_runs set state='rejected',approval_code=null,updated_at=%s
                            where state='pending_approval' and approval_expires_at<=%s""",
            (now, now),
        )
    for _ in range(32):
        code = f"{randbelow(10000):04d}" if required else None
        await cur.execute(
            """insert into action_runs(alert_id,runbook_id,step_index,action,params,approval,
            state,approval_code,approval_expires_at,requested_by,resolves_alert)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning *""",
            (
                alert["id"],
                alert["runbook_id"],
                step["index"],
                step["action"],
                Jsonb(step["params"]),
                step["approval"],
                "pending_approval" if required else "requested",
                code,
                now + timedelta(minutes=10) if required else None,
                actor,
                step.get("resolves_alert", False),
            ),
        )
        row = await cur.fetchone()
        if row:
            break
    else:
        raise HTTPException(503, "Approval codes busy")
    if required:
        text = f"Approve {step['action']} for alert {alert['id']}? Reply YES {code} or NO {code} within 10 minutes."
        msg = RenderedMessage(title="Runbook approval", body=text, short=text)
        intents = [
            NotificationIntent(
                alert_id=alert["id"],
                kind="approval_request",
                recipient=u,
                channel="sms",
                contact=contact,
                rendered=msg,
                priority=1,
                dedup_key=f"{alert['id']}:approval_request:{u}:sms:{row['id']}",
            )
            for u, contact in contacts
            if contact
        ]
        await enqueue(cur, intents, alert["team_id"])
    await audit(cur, actor, "action.request", "alert", alert["id"], {"run_id": row["id"]})
    await changed(cur, row, alert["team_id"])
    return public(row)


async def decide(cur, run_id, actor, approve, settings, code=None):
    await cur.execute(
        """select r.*,a.team_id from action_runs r join alerts a on a.id=r.alert_id
                         where r.id=%s for update of r""",
        (run_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Action run not found")
    team = by_id(settings.directory["teams"]).get(row["team_id"], {})
    if actor not in team.get("approvers", []):
        raise HTTPException(403, "Team approver required")
    if row["state"] != "pending_approval" or row["approval_expires_at"] <= SYSTEM.now():
        raise HTTPException(409, "Approval expired or already decided")
    if code is not None and code != row["approval_code"]:
        raise HTTPException(403, "Invalid approval code")
    await cur.execute(
        """update action_runs set state=%s,approved_by=%s,approval_code=null,updated_at=now()
                         where id=%s returning *""",
        ("approved" if approve else "rejected", actor, run_id),
    )
    updated = await cur.fetchone()
    await audit(
        cur,
        actor,
        "action.approve" if approve else "action.reject",
        "alert",
        row["alert_id"],
        {"run_id": run_id},
    )
    await changed(cur, updated, row["team_id"])
    return public(updated)


async def execute(db, run_id, actor, settings):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """update action_runs set state='running',updated_at=now()
                         where id=%s and state in ('approved','requested') returning *""",
            (run_id,),
        )
        run = await cur.fetchone()
        if not run:
            raise HTTPException(409, "Run already claimed")
        await cur.execute(
            """select a.*,e.fields as event_fields from alerts a left join events e
            on e.id=a.representative_event_id where a.id=%s""",
            (run["alert_id"],),
        )
        alert = await cur.fetchone()
        await changed(cur, run, alert["team_id"])
    ctx = ActionContext(
        alert=alert, event_fields=alert.get("event_fields") or {}, actor=actor, run_id=run_id
    )
    try:
        result = await REGISTRY[run["action"]].execute(run["params"], ctx)
    except (KeyError, ValueError, httpx.HTTPError) as exc:
        result = ActionResult(ok=False, error=str(exc))
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if run["action"] == "escalate" and result.ok:
            team = result.output["escalate_to"]
            if team not in by_id(settings.directory["teams"]):
                result = ActionResult(ok=False, error="Unknown escalation team")
            else:
                rule = {
                    "id": alert["rule_id"],
                    "route": [
                        {"to": "oncall:" + team, "channels": ["sms"]},
                        {"to": "team:" + team, "channels": ["ui"]},
                    ],
                }
                routing = plan(
                    alert, "escalation", rule, ctx.event_fields, settings.directory, [], {}, SYSTEM
                )
                await enqueue(cur, routing.intents, team)
        await cur.execute(
            """update action_runs set state=%s,result=%s,updated_at=now()
            where id=%s returning *""",
            ("succeeded" if result.ok else "failed", Jsonb(result.model_dump()), run_id),
        )
        row = await cur.fetchone()
        await audit(
            cur, actor, "action.execute", "alert", alert["id"], {"run_id": run_id, "ok": result.ok}
        )
        if result.ok and (run["resolves_alert"] or result.resolves_alert):
            await cur.execute(
                """update alerts set status='resolved',resolved_by='system',resolved_at=now(),
                resolve_reason='runbook',updated_at=now() where id=%s and status<>'resolved' returning *""",
                (alert["id"],),
            )
            updated = await cur.fetchone()
            if updated:
                await audit(
                    cur, "system", "alert.resolve", "alert", alert["id"], {"run_id": run_id}
                )
                await alert_changed(cur, updated)
        await changed(cur, row, alert["team_id"])
        return public(row)
