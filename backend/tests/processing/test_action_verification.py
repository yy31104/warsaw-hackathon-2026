import httpx
import pytest

from alerting.runbooks.actions import network

from .test_worker import Settings, process_batch, seed


def receiver(monkeypatch, receipt):
    calls = []
    original = httpx.AsyncClient

    def respond(req):
        calls.append((req.method, req.url.path, req.headers.get("Idempotency-Key")))
        if len(calls) == 1:
            raise httpx.ReadTimeout("reply lost", request=req)
        if req.method == "GET":
            return httpx.Response(404) if receipt is None else httpx.Response(200, json=receipt)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        network.httpx, "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    return calls


async def start_unknown(api, database, actor="sam"):
    await seed(database)
    await process_batch(database, Settings.load())
    response = await api.post(
        "/v1/alerts/1/actions", json={"step_index": 1}, headers={"X-User": actor},
    )
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["state"] == "unknown"
    return f"/v1/action-runs/{run['id']}"


async def test_unknown_run_blocks_new_start_and_confirmed_retry_before_receiver_io(api, database, monkeypatch):
    calls = receiver(monkeypatch, None)
    base = await start_unknown(api, database)
    headers = {"X-User": "sam"}
    assert (await api.post("/v1/alerts/1/actions", json={"step_index": 1}, headers=headers)).status_code == 409
    for confirm in (False, True):
        response = await api.post(base + "/retry", json={"confirm": confirm}, headers=headers)
        assert response.status_code == 409, response.text
    assert calls == [("POST", "/workflows/WF-0/retry", "action-1")]


@pytest.mark.parametrize(("actor", "state"), [("sam", "succeeded"), ("lee", "failed")])
async def test_requester_or_approver_reconciles_terminal_receipt_without_reexecuting(
    api, database, monkeypatch, actor, state,
):
    ok = state == "succeeded"
    calls = receiver(monkeypatch, {
        "state": state, "result": {"ok": ok, "output": {"job": 17}, "resolves_alert": ok},
    })
    base = await start_unknown(api, database)
    step = (await api.get("/v1/alerts/1/runbook")).json()["steps"][1]
    assert (step["requested_by"], step["approvers"], step["execution_count"]) == ("sam", ["lee"], 1)
    response = await api.post(base + "/reconcile", headers={"X-User": actor})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == state
    assert response.json()["result"]["output"] == {"job": 17}
    assert (await api.post(base + "/reconcile", headers={"X-User": actor})).status_code == 409
    assert calls == [
        ("POST", "/workflows/WF-0/retry", "action-1"), ("GET", "/executions/action-1", None),
    ]
    reloaded = (await api.get("/v1/alerts/1/runbook")).json()["steps"][1]
    assert (reloaded["state"], reloaded["execution_count"]) == (state, 1)
    async with database.connection() as conn:
        audit = await (await conn.execute(
            """select actor,action,detail->>'receiver_state' from audit_log where detail->>'run_id'='1'
               and action in ('action.execute','action.reconcile','action.retry') order by id"""
        )).fetchall()
        alert = await (await conn.execute("select status from alerts where id=1")).fetchone()
    assert audit == [("sam", "action.execute", None), (actor, "action.reconcile", state)]
    assert alert == ("resolved" if ok else "open",)


@pytest.mark.parametrize(("path", "receipt", "recorded"), [
    ("reconcile", None, "not_found"), ("check-result", None, "not_found"),
    ("reconcile", {"state": "running"}, "running"), ("reconcile", {"state": "unknown"}, "unknown"),
])
async def test_nonterminal_or_missing_receipt_leaves_run_unknown_and_audits_lookup(
    api, database, monkeypatch, path, receipt, recorded,
):
    calls = receiver(monkeypatch, receipt)
    base = await start_unknown(api, database)
    response = await api.post(base + "/" + path, headers={"X-User": "lee"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "unknown"
    assert "receipt" in response.json()["message"].lower()
    async with database.connection() as conn:
        row = await (await conn.execute(
            """select r.state,r.result->>'error',l.actor,l.detail->>'receiver_state'
               from action_runs r join audit_log l on l.detail->>'run_id'=r.id::text
               where l.action='action.reconcile'"""
        )).fetchone()
    assert row == ("unknown", "Receiver timed out; check execution before retry", "lee", recorded)
    assert [method for method, _, _ in calls] == ["POST", "GET"]


async def test_reconciliation_rejects_unrelated_team_member_and_other_team_before_lookup(
    api, database, monkeypatch,
):
    calls = receiver(monkeypatch, None)
    base = await start_unknown(api, database, actor="lee")
    for actor in ("sam", "pat"):
        for path in ("reconcile", "check-result"):
            response = await api.post(base + "/" + path, headers={"X-User": actor})
            assert response.status_code == 403, (actor, path, response.text)
    assert calls == [("POST", "/workflows/WF-0/retry", "action-1")]
    async with database.connection() as conn:
        assert (0,) == await (await conn.execute(
            "select count(*) from audit_log where action in ('action.verify','action.reconcile','action.retry')"
        )).fetchone()


async def test_confirmed_retry_still_verifies_and_uses_fresh_run_after_confirmed_failure(
    api, database, monkeypatch,
):
    calls = receiver(monkeypatch, {"state": "failed", "result": {"ok": False, "error": "rejected"}})
    base = await start_unknown(api, database)
    headers = {"X-User": "sam"}
    reconciled = await api.post(base + "/reconcile", headers=headers)
    assert reconciled.json()["state"] == "failed", reconciled.text
    rerun = await api.post(base + "/retry", headers=headers, json={"confirm": True})
    assert rerun.status_code == 200 and rerun.json()["state"] == "succeeded", rerun.text
    assert [key for method, _, key in calls if method == "POST"] == ["action-1", "action-2"]
    assert [method for method, _, _ in calls] == ["POST", "GET", "GET", "POST"]
