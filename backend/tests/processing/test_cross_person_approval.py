import asyncio
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from alerting.api.app import app
from alerting.db import pool
from alerting.runbooks.actions import network


def session(actor):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-User": actor}
    )


@pytest.fixture
async def alerts(api, database, monkeypatch):
    # The processing fixture scopes incident routes. Config routes use the pool dependency.
    monkeypatch.setitem(app.dependency_overrides, pool, lambda: database)
    document = {
        "id": "synthetic-approval",
        "owner": "user:sam",
        "format": "yaml-steps",
        "content": """steps:
  - {action: retry_workflow, approval: required, workflowId: synthetic-primary}
  - {action: retry_workflow, approval: required, workflowId: synthetic-backup}
""",
    }
    published = await api.post("/v1/runbooks", json=document, headers={"X-User": "sam"})
    assert published.status_code == 201, published.text
    async with database.connection() as conn:
        created = await (await conn.execute(
            """insert into alerts(rule_id,rule_version,alert_key,severity,team_id,runbook_id)
               select 'synthetic',1,team,'error',team,%s from unnest(%s::text[]) as t(team)
               returning team_id,id""",
            (document["id"], ["settlements", "rates"]),
        )).fetchall()
    return {team: f"/v1/alerts/{alert_id}" for team, alert_id in created}


@pytest.fixture
async def receiver(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    from mocks.mock_ops.service import create_app

    ops = create_app(tmp_path / "receipts.sqlite3")
    requests = []

    @ops.middleware("http")
    async def count_workflow_posts(request, call_next):
        if request.method == "POST" and request.url.path.startswith("/workflows/"):
            requests.append((request.url.path, request.headers["Idempotency-Key"]))
        return await call_next(request)

    transport = httpx.ASGITransport(app=ops)
    monkeypatch.setenv("MOCK_OPS_URL", "http://mock-ops:8200")
    # Only the runbook's provider uses this transport. Each operator has a separate API client.
    monkeypatch.setattr(network, "httpx", SimpleNamespace(AsyncClient=partial(httpx.AsyncClient, transport=transport)))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock-ops:8200") as client:
        yield client, requests


async def request_action(client, alert, index=0):
    response = await client.post(f"{alert}/actions", json={"step_index": index})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["state"] == "pending_approval" and "approval_code" not in run
    return run


async def assert_team_count(client, database, team, expected):
    response = await client.get("/v1/stats/pending-approvals", params={"team": team} if team else {})
    assert response.status_code == 200, response.text
    async with database.connection() as conn:
        count, = await (await conn.execute(
            """select count(*) from action_runs r join alerts a on a.id=r.alert_id
               where (%s::text is null or a.team_id=%s) and r.state='pending_approval'
                 and r.approval_expires_at>now()""",
            (team, team),
        )).fetchone()
    assert response.json()["pending_approval_count"] == count == expected


async def test_fresh_approver_discovers_and_executes_the_request_once(alerts, database, receiver):
    alert = alerts["settlements"]
    ops, calls = receiver
    async with session("sam") as sam:
        run = await request_action(sam, alert)
        key = f"action-{run['id']}"
        assert (await ops.get(f"/executions/{key}")).status_code == 404
        async with session("lee") as lee:
            detail = await lee.get(alert)
            detail.raise_for_status()
            (pending,) = detail.json()["pending_actions"]
            assert pending["run_id"] == run["id"]
            assert pending["action"] == "retry_workflow" and pending["status"] == "pending_approval"
            assert pending["requested_by"] == "sam" and pending["approvers"] == ["lee"]
            assert datetime.fromisoformat(pending["created_at"]) == datetime.fromisoformat(run["created_at"])
            assert "approval_code" not in pending
            await assert_team_count(lee, database, "settlements", 1)
            before = (await lee.get(f"{alert}/runbook")).json()["steps"][0]
            assert (before["run_id"], before["state"], before["execution_count"]) == (pending["run_id"], "pending_approval", 0)

            # Lee obtains the decision URL exclusively from a fresh read.
            approve = f"/v1/action-runs/{pending['run_id']}/approve"
            assert (await sam.post(approve)).status_code == 403
            async with session("rita") as rita:
                assert (await rita.post(approve)).status_code == 403
            assert calls == []
            accepted = await lee.post(approve)
            assert accepted.status_code == 200, accepted.text
            assert (accepted.json()["state"], accepted.json()["approved_by"]) == ("succeeded", "lee")
            assert (await lee.post(approve)).status_code == 409

        assert (await sam.get(alert)).json()["pending_actions"] == []
        await assert_team_count(sam, database, "settlements", 0)
        first, other = (await sam.get(f"{alert}/runbook")).json()["steps"]
        assert (first["run_id"], first["state"], first["approved_by"], first["execution_count"]) == (
            run["id"], "succeeded", "lee", 1,
        )
        assert other["run_id"] is None and other["execution_count"] == 0
    receipt = await ops.get(f"/executions/{key}")
    receipt.raise_for_status()
    assert receipt.json()["state"] == "succeeded"
    assert receipt.json()["result"]["output"]["attempt"] == 1
    assert calls == [("/workflows/synthetic-primary/retry", key)]
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select state,requested_by,approved_by from action_runs where id=%s", (run["id"],)
        )).fetchone() == ("succeeded", "sam", "lee")
        assert await (await conn.execute(
            "select actor,action from audit_log where detail->>'run_id'=%s order by id", (str(run["id"]),)
        )).fetchall() == [("sam", "action.request"), ("lee", "action.approve"), ("lee", "action.execute")]


async def test_team_count_and_pending_ids_distinguish_repeated_actions_and_expiry(alerts, database):
    alert = alerts["settlements"]
    async with session("sam") as sam, session("rita") as rita:
        await request_action(rita, alerts["rates"])
        first = await request_action(sam, alert)
        second = await request_action(sam, alert)
        expiring = await request_action(sam, alert, index=1)
        await assert_team_count(sam, database, "settlements", 3)
        await assert_team_count(rita, database, "rates", 1)
        await assert_team_count(sam, database, None, 4)
        all_teams = await sam.get("/v1/stats/pending-approvals", params={"team": ""})
        assert all_teams.json() == {"pending_approval_count": 4}
        async with database.connection() as conn:
            await conn.execute("update action_runs set approval_expires_at=now()-interval '1s' where id=%s", (expiring["id"],))
        async with session("lee") as lee:
            pending = (await lee.get(alert)).json()["pending_actions"]
            assert {row["run_id"] for row in pending} == {first["id"], second["id"]}
            await assert_team_count(lee, database, "settlements", 2)
            assert (await lee.post(f"/v1/action-runs/{expiring['id']}/approve")).status_code == 409
            detail = (await lee.get(f"{alert}/runbook")).json()["steps"]
            assert (detail[1]["run_id"], detail[1]["state"], detail[1]["execution_count"]) == (expiring["id"], "expired", 0)
            assert detail[0]["action"] == detail[1]["action"] == "retry_workflow"
            for row in pending:
                rejected = await lee.post(f"/v1/action-runs/{row['run_id']}/reject")
                assert rejected.status_code == 200, rejected.text
            await assert_team_count(lee, database, "settlements", 0)
        await assert_team_count(rita, database, "rates", 1)
        await assert_team_count(sam, database, None, 1)


async def test_two_approver_sessions_racing_execute_once(alerts, receiver):
    alert = alerts["settlements"]
    ops, calls = receiver
    async with session("sam") as sam:
        run = await request_action(sam, alert)
        async with session("lee") as first, session("lee") as second:
            first_read, second_read = await asyncio.gather(first.get(alert), second.get(alert))
            first_id, second_id = [response.json()["pending_actions"][0]["run_id"] for response in (first_read, second_read)]
            assert first_id == second_id == run["id"]
            responses = await asyncio.gather(
                first.post(f"/v1/action-runs/{first_id}/approve"),
                second.post(f"/v1/action-runs/{second_id}/approve"),
            )
        assert sorted(response.status_code for response in responses) == [200, 409]
        step = (await sam.get(f"{alert}/runbook")).json()["steps"][0]
        assert (step["state"], step["approved_by"], step["execution_count"]) == ("succeeded", "lee", 1)
    key = f"action-{run['id']}"
    assert calls == [("/workflows/synthetic-primary/retry", key)]
    receipt = (await ops.get(f"/executions/{key}")).json()
    assert receipt["state"] == "succeeded" and receipt["result"]["output"]["attempt"] == 1
