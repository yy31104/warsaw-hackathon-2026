import asyncio

import pytest
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from alerting.contracts import ActionResult
from alerting.runbooks.actions import REGISTRY
from tests.processing import conftest as postgres
from tests.processing.test_worker import Settings, process_batch, seed

api = postgres.api
database = postgres.database


async def new_alert(db, *, team="settlements", service="queue", code="STALLED", schema="jobs.v2"):
    async with db.connection() as conn:
        (event_id,) = await (
            await conn.execute(
                """insert into events(source_id, transport, payload_hash, raw, fields, service,
                                  schema_id, schema_name, schema_version)
               values ('memory-test','http','hash','{}',%s,%s,'custom',%s,3) returning id""",
                (Jsonb({"$error_code": code}), service, schema),
            )
        ).fetchone()
        (alert_id,) = await (
            await conn.execute(
                """insert into alerts(rule_id, rule_version, alert_key, team_id, representative_event_id)
               values ('memory-test',1,%s,%s,%s) returning id""",
                (str(event_id), team, event_id),
            )
        ).fetchone()
        return alert_id


async def resolve(api, alert_id, note, actor="sam", **extra):
    return await api.post(
        f"/v1/alerts/{alert_id}/resolve-with-note",
        headers={"X-User": actor},
        json={"note": note, **extra},
    )


async def hints(api, alert_id, actor="sam"):
    response = await api.get(f"/v1/alerts/{alert_id}/similar", headers={"X-User": actor})
    assert response.status_code == 200, response.text
    return response.json()["hints"]


async def test_verified_card_snapshots_fixture_and_audited_attempts(api, database, monkeypatch):
    await seed(database)
    await process_batch(database, Settings.load())
    prefix = "/v1/alerts/1"
    async with database.connection() as conn:
        await conn.execute("update events set schema_version=3")
        await conn.execute("update alerts set runbook_version=7")

    async def failed_retry(params, ctx):
        return ActionResult(ok=False, error="private provider detail")

    monkeypatch.setattr(REGISTRY["retry_workflow"], "execute", failed_retry)
    failed = (
        await api.post(prefix + "/actions", json={"step_index": 1}, headers={"X-User": "sam"})
    ).json()
    approval = (
        await api.post(prefix + "/actions", json={"step_index": 2}, headers={"X-User": "sam"})
    ).json()
    assert failed["state"] == "failed" and approval["state"] == "pending_approval"
    approval_url = f"/v1/action-runs/{approval['id']}/approve"
    assert (await api.post(approval_url, headers={"X-User": "lee"})).json()["state"] == "succeeded"
    async with database.connection() as conn:
        await conn.execute(
            """insert into action_runs(alert_id, action, approval, requested_by, state)
               values (1, 'unverified_import', 'none', 'sam', 'succeeded')"""
        )

    response = await resolve(
        api, 1, "Recovered; contact person@example.com", verified_resolution="Queue drained"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    # A different schema can still match the service + error code. Later changes to the event
    # or attached runbook do not rewrite the historical evidence.
    other = await new_alert(database, service="order-settlement", code="TIMEOUT", schema="other")
    async with database.connection() as conn:
        await conn.execute("update events set schema_version=8, service='unrelated' where id=1")
        await conn.execute("update alerts set runbook_version=9 where id=1")
    (card,) = await hints(api, other)
    assert card["alert_id"] == 1 and card["match_reason"] == "service_error_code"
    assert (
        card["service"],
        card["error_code"],
        card["schema_id"],
        card["schema"],
        card["schema_version"],
    ) == ("order-settlement", "TIMEOUT", "workflow", "WORKFLOW_FAILED", 3)
    assert (card["runbook_id"], card["runbook_version"]) == ("settlement-timeout", 7)
    assert (
        card["verified_resolution"] == "Queue drained"
        and card["notes"] == "Recovered; contact <email>"
    )
    assert card["confirmed_by"] == "sam"
    assert [(a["run_id"], a["action"], a["ok"]) for a in card["actions_attempted"]] == [
        (failed["id"], "retry_workflow", False),
        (approval["id"], "escalate", True),
    ]
    assert all(set(a) == {"run_id", "action", "ok"} for a in card["actions_attempted"])
    assert "private provider" not in str(card) and "person@example.com" not in str(card)


async def test_hint_order_partial_keys_and_team_boundaries(api, database):
    exact = await new_alert(database, schema="older.format")
    weak = await new_alert(database, service=None)
    foreign = await new_alert(database, team="rates")
    unrelated = await new_alert(database, code=None, schema="other.format")
    subject = await new_alert(database)
    for alert_id in (exact, weak, unrelated):
        assert (await resolve(api, alert_id, "Operator verified the recovery")).status_code == 200
    assert (await resolve(api, foreign, "Rates team private note", actor="rita")).status_code == 200

    assert [(card["alert_id"], card["match_reason"]) for card in await hints(api, subject)] == [
        (exact, "service_error_code"),
        (weak, "schema"),
    ]
    assert await hints(api, exact) == []
    partial = await new_alert(database, service=None)
    assert [(c["alert_id"], c["match_reason"]) for c in await hints(api, partial)] == [
        (weak, "schema")
    ]
    assert (
        await api.get(f"/v1/alerts/{subject}/similar", headers={"X-User": "rita"})
    ).status_code == 403
    assert (
        await api.get(f"/v1/alerts/{foreign}/similar", headers={"X-User": "sam"})
    ).status_code == 403
    assert (await api.get(f"/v1/alerts/{subject}/similar")).status_code == 422
    assert (await api.get("/v1/alerts/99999/similar", headers={"X-User": "sam"})).status_code == 404


async def test_only_resolving_human_can_confirm_after_ordinary_resolve(api, database):
    subject = await new_alert(database)
    manual = await new_alert(database)
    automatic = await new_alert(database)
    async with database.connection() as conn:
        await conn.execute(
            """update alerts set status='resolved', resolved_by='system', resolved_at=now(),
                                 resolve_reason='resolve_when' where id=%s""",
            (automatic,),
        )
    assert (await resolve(api, automatic, "A machine closed this alert")).status_code == 409
    assert (await resolve(api, manual, "unauthorized", actor="rita")).status_code == 403
    assert (await resolve(api, manual, " \n")).status_code == 422
    assert (await api.get(f"/v1/alerts/{manual}")).json()["status"] == "open"
    ordinary = await api.post(f"/v1/alerts/{manual}/resolve", headers={"X-User": "sam"})
    assert ordinary.json()["status"] == "resolved" and await hints(api, subject) == []
    assert (await resolve(api, manual, "A teammate closed it", actor="lee")).status_code == 409
    async with database.connection() as conn:
        # A later execution must not become part of the historical resolution on a delayed note.
        await conn.execute(
            """with late_run as (
                   insert into action_runs(alert_id, action, approval, requested_by, state)
                   values (%s,'late_step','none','sam','succeeded') returning id
               ) insert into audit_log(actor, action, target_kind, target_id, detail)
                 select 'sam','action.execute','alert',%s,jsonb_build_object('run_id',id,'ok',true)
                 from late_run""",
            (manual, str(manual)),
        )
    assert (await resolve(api, manual, "Restart confirmed")).status_code == 200
    (card,) = await hints(api, subject)
    assert (card["alert_id"], card["verified_resolution"], card["actions_attempted"]) == (
        manual,
        "Restart confirmed",
        [],
    )


async def test_concurrent_note_retries_are_idempotent_and_conflicts_preserve_evidence(
    api, database
):
    incident = await new_alert(database)
    subject = await new_alert(database)
    attempts = await asyncio.gather(*(resolve(api, incident, "Queue drained") for _ in range(2)))
    assert [r.status_code for r in attempts] == [200, 200]
    assert (await resolve(api, incident, "Overwrite the first resolution")).status_code == 409
    assert (await resolve(api, incident, "Queue drained", actor="lee")).status_code == 409
    assert [c["verified_resolution"] for c in await hints(api, subject)] == ["Queue drained"]
    async with database.connection() as conn:
        assert await (await conn.execute("select count(*) from incident_memory")).fetchone() == (1,)
        assert await (
            await conn.execute(
                "select count(*) from audit_log where action='alert.resolve' and target_id=%s",
                (str(incident),),
            )
        ).fetchone() == (1,)


async def test_registry_runbook_version_is_taken_as_of_resolution(api, database):
    legacy = await new_alert(database)
    subject = await new_alert(database)
    async with database.connection() as conn:
        await conn.execute("update alerts set runbook_id='custom-runbook' where id=%s", (legacy,))
        await conn.execute(
            """insert into config_objects(kind,id,version,spec,content_hash,origin,active,created_at)
               values ('runbook','custom-runbook',2,'{}','old','ui',false,now() - interval '1 day'),
                      ('runbook','custom-runbook',3,'{}','new','ui',true,now() + interval '1 day')"""
        )
    assert (await resolve(api, legacy, "Release reverted")).status_code == 200
    (card,) = await hints(api, subject)
    assert card["runbook_id"] == "custom-runbook" and card["runbook_version"] == 2


async def test_file_bootstrap_version_is_used_only_when_observed_at_resolution(api, database):
    fresh = await new_alert(database)
    historical = await new_alert(database)
    subject = await new_alert(database)
    async with database.connection() as conn:
        await conn.execute(
            "update alerts set runbook_id='settlement-timeout' where id in (%s, %s)",
            (fresh, historical),
        )
    assert (await resolve(api, fresh, "Resolved against the current file")).status_code == 200
    ordinary = await api.post(f"/v1/alerts/{historical}/resolve", headers={"X-User": "sam"})
    assert ordinary.status_code == 200
    assert (await resolve(api, historical, "Resolution recorded later")).status_code == 200
    assert {c["alert_id"]: c["runbook_version"] for c in await hints(api, subject)} == {
        fresh: 1,
        historical: None,
    }


async def test_failed_card_write_rolls_back_resolution_and_audit(api, database):
    incident = await new_alert(database)
    async with database.connection() as conn:
        await conn.execute(
            "alter table incident_memory add check (notes <> 'Simulate write failure')"
        )
    with pytest.raises(CheckViolation):
        await resolve(api, incident, "Simulate write failure")
    assert (await api.get(f"/v1/alerts/{incident}")).json()["status"] == "open"
    async with database.connection() as conn:
        assert await (await conn.execute("select count(*) from audit_log")).fetchone() == (0,)
    assert (await resolve(api, incident, "Recovered on retry")).status_code == 200
