from .test_worker import Settings, process_batch, seed


async def test_approval_flow(api, database):
    await seed(database)
    await process_batch(database, Settings.load())
    path = "/v1/alerts/1"
    steps = (await api.get(path + "/runbook")).json()["steps"]
    assert steps[1]["params"]["workflowId"] == "WF-0"
    response = await api.post(path + "/actions", json={"step_index": 2}, headers={"X-User": "sam"})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["state"] == "pending_approval" and "approval_code" not in run
    url = f"/v1/action-runs/{run['id']}/approve"
    assert (await api.post(url, headers={"X-User": "rita"})).status_code == 403
    assert (await api.post(url, headers={"X-User": "lee"})).json()["state"] == "succeeded"
    assert (await api.post(url, headers={"X-User": "lee"})).status_code == 409
    async with database.connection() as conn:
        assert await (
            await conn.execute(
                "select distinct recipient from notifications where kind='escalation' and channel='sms'"
            )
        ).fetchone() == ("cara",)
        assert await (
            await conn.execute(
                "select count(*) from audit_log where actor='lee' and action in ('action.execute','action.approve')"
            )
        ).fetchone() == (2,)


async def test_mock_ops_and_suggested_escalation(api, database, monkeypatch):
    import httpx
    from alerting.runbooks.actions import network

    client_type = httpx.AsyncClient
    requests = []

    def receive(req):
        requests.append(req.url.path)
        if req.url.path == "/health":
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(503)

    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(receive), **kw),
    )
    await seed(database)
    await process_batch(database, Settings.load())
    for step in [1, 1]:
        res = await api.post(
            "/v1/alerts/1/actions", json={"step_index": step}, headers={"X-User": "sam"}
        )
        assert res.json()["state"] == "failed"
    assert (await api.get("/v1/alerts/1/runbook")).json()["steps"][2]["suggested"]
    assert (
        await api.post("/v1/alerts/1/actions", json={"step_index": 0}, headers={"X-User": "sam"})
    ).json()["state"] == "succeeded"
    assert (await api.get("/v1/alerts/1")).json()["resolved_by"] == "system"
    assert requests == ["/workflows/WF-0/retry", "/workflows/WF-0/retry", "/health"]
