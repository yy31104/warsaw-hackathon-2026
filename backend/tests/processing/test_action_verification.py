import httpx
import pytest
from alerting.runbooks.actions import network

from .test_worker import Settings, process_batch, seed


@pytest.mark.parametrize("receiver_has_result", [True, False])
async def test_unknown_checks_receiver_before_retry(
    api, database, monkeypatch, receiver_has_result
):
    calls = []
    original = httpx.AsyncClient

    def provider(req):
        calls.append((req.method, req.url.path, req.headers.get("Idempotency-Key")))
        if len(calls) == 1:
            raise httpx.ReadTimeout("reply lost")
        if req.url.path.startswith("/executions/"):
            if receiver_has_result:
                return httpx.Response(
                    200, json={"state": "succeeded", "result": {"ok": True, "output": {"job": 17}}}
                )
            return httpx.Response(404)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(provider), **kw),
    )
    await seed(database)
    await process_batch(database, Settings.load())
    headers = {"X-User": "sam"}
    start = "/v1/alerts/1/actions"
    run = (await api.post(start, json={"step_index": 1}, headers=headers)).json()
    assert run["state"] == "unknown"
    assert (await api.post(start, json={"step_index": 1}, headers=headers)).status_code == 409
    base = f"/v1/action-runs/{run['id']}"
    assert (
        await api.post(base + "/retry", json={"confirm": False}, headers=headers)
    ).status_code == 409
    assert len(calls) == 1
    res = await api.post(base + "/retry", json={"confirm": True}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "succeeded"
    assert calls[1][:2] == ("GET", "/executions/action-1")
    assert [key for method, path, key in calls if method == "POST"] == ["action-1"] * (
        1 if receiver_has_result else 2
    )
