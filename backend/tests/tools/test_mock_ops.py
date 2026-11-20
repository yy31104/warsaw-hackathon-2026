import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from alerting.contracts import ActionResult
from alerting.runbooks.actions import network
from alerting.runbooks.actions.http_get import HealthCheck
from alerting.runbooks.actions.open_ticket import Ticket


@pytest.fixture
def mock_ops(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    from mocks.mock_ops import service

    return service


@pytest.fixture
def receiver(mock_ops, tmp_path):
    return mock_ops.create_app(tmp_path / "ops.sqlite3")


@pytest.fixture
async def client(receiver):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver), base_url="http://mock-ops:8200"
    ) as client:
        yield client


def headers(key):
    return {"Idempotency-Key": key}


@pytest.mark.parametrize(
    ("status", "output", "state", "resolves"),
    [
        (201, {"ticket_id": "one"}, "succeeded", False),
        (200, {"ok": True, "resolves_alert": True}, "succeeded", True),
        (200, {"ok": False, "resolves_alert": False}, "failed", False),
        (503, {"ok": False}, "failed", False),
    ],
)
def test_receipt_describes_recorded_operation(mock_ops, status, output, state, resolves):
    output["simulated"] = True
    receipt = mock_ops.execution_receipt(status, output)
    assert receipt["simulated"] is True
    assert receipt["state"] == state
    result = ActionResult.model_validate(receipt["result"])
    assert result.ok == (state == "succeeded")
    assert result.output == output
    assert result.resolves_alert is resolves


async def test_toggle_and_keyed_health_replay(client):
    assert (await client.get("/health")).json()["healthy"] is True
    down = await client.post("/toggle", headers=headers("toggle-1"))
    assert down.json()["healthy"] is False
    first = await client.get("/health", headers=headers("action-1"))
    assert first.json()["simulated"] is True
    assert first.json()["ok"] is False

    assert (await client.post("/toggle", headers=headers("toggle-2"))).json()["healthy"] is True
    assert (await client.post("/toggle", headers=headers("toggle-1"))).json() == down.json()
    assert (await client.get("/health")).json()["healthy"] is True
    assert (await client.get("/health", headers=headers("action-1"))).json() == first.json()
    receipt = (await client.get("/executions/action-1")).json()
    assert receipt["state"] == "failed"
    assert receipt["result"]["output"] == first.json()


async def test_retry_budget_counts_distinct_calls_per_workflow(client):
    await client.post("/toggle", json={"healthy": False}, headers=headers("down"))
    configured = await client.post(
        "/config", json={"fail_n": 2}, headers=headers("scenario")
    )
    assert configured.is_success
    for attempt in range(1, 4):
        response = await client.post("/workflows/alpha/retry", headers=headers(f"action-{attempt}"))
        assert response.status_code == (503 if attempt <= 2 else 200)
        assert response.json()["ok"] is (attempt == 3)
        assert response.json()["attempt"] == attempt
        replay = await client.post("/workflows/alpha/retry", headers=headers(f"action-{attempt}"))
        assert (replay.status_code, replay.json()) == (response.status_code, response.json())
        receipt = (await client.get(f"/executions/action-{attempt}")).json()
        assert receipt["state"] == ("failed" if attempt <= 2 else "succeeded")
        assert receipt["result"]["output"] == response.json()

    other = await client.post("/workflows/beta/retry", headers=headers("action-4"))
    assert other.status_code == 503
    assert other.json()["attempt"] == 1
    await client.post("/config", json={"fail_n": 0}, headers=headers("enable"))
    replay = await client.post("/config", json={"fail_n": 2}, headers=headers("scenario"))
    assert replay.json() == configured.json()
    assert (await client.get("/health")).json()["healthy"] is False
    assert (await client.post("/workflows/new/retry", headers=headers("action-5"))).is_success


async def test_invalid_config_never_changes_the_budget_or_records_an_execution(client):
    for invalid in ({"fail_n": -1}, {"fail_n": True}, {"typo": 2}):
        response = await client.post("/config", json=invalid, headers=headers("invalid"))
        assert response.status_code == 422
        assert response.json()["simulated"] is True
    assert (await client.get("/executions/invalid")).status_code == 404
    assert (await client.post("/workflows/alpha/retry", headers=headers("first"))).is_success


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/toggle", {}),
        ("/config", {"fail_n": 2}),
        ("/workflows/alpha/retry", None),
        ("/services/alpha/restart", None),
        ("/tickets", {"title": "Investigate pool"}),
    ],
)
async def test_every_mutation_requires_a_nonblank_key(client, path, body):
    for invalid_headers in ({}, headers("   ")):
        response = await client.post(path, json=body, headers=invalid_headers)
        assert response.status_code == 422
        assert response.json()["simulated"] is True


async def test_concurrent_duplicates_and_conflicting_key_reuse(client):
    path = "/workflows/concurrent/retry"
    duplicates = await asyncio.gather(
        *(client.post(path, headers=headers("same")) for _ in range(8))
    )
    assert all(item.status_code == 200 and item.json()["attempt"] == 1 for item in duplicates)
    assert (await client.post(path, headers=headers("next"))).json()["attempt"] == 2
    for new_path in ("/workflows/other/retry", "/services/concurrent/restart"):
        conflict = await client.post(new_path, headers=headers("same"))
        assert conflict.status_code == 409
        assert conflict.json()["simulated"] is True
    recorded = (await client.get("/executions/same")).json()["result"]["output"]
    assert recorded == duplicates[0].json()


async def test_restart_and_ticket_ids_survive_receiver_restart(mock_ops, tmp_path, client):
    calls = (
        ("/services/payments/restart", "restart", None, "restart_id"),
        ("/tickets", "ticket", {"title": "Investigate pool", "service": "payments"}, "ticket_id"),
    )
    results = []
    for path, key, body, id_field in calls:
        response = await client.post(path, headers=headers(key), json=body)
        assert response.is_success and response.json().get(id_field)
        assert response.json()["simulated"] is True
        results.append(response)

    reopened = mock_ops.create_app(tmp_path / "ops.sqlite3")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened), base_url="http://counterparty-service:8200"
    ) as other:
        for (path, key, body, _), original in zip(calls, results, strict=True):
            replay = await other.post(path, headers=headers(key), json=body)
            assert (replay.status_code, replay.json()) == (original.status_code, original.json())
            receipt = (await other.get(f"/executions/{key}")).json()
            assert receipt["result"]["output"] == original.json()
        conflict = await other.post("/tickets", headers=headers("ticket"), json={"title": "Other"})
        assert conflict.status_code == 409
        missing = await other.get("/executions/unseen")
        assert missing.status_code == 404 and missing.json()["simulated"] is True


async def test_read_only_investigation_targets(client):
    params = {"service": "payments"}
    pool = (await client.get("/metrics/pool", params=params)).json()
    assert pool["simulated"] is True and pool["service"] == "payments"
    assert 0 <= pool["active"] <= pool["capacity"]
    deployments = (await client.get("/deployments", params=params)).json()
    assert deployments["simulated"] is True
    assert deployments["items"][0]["service"] == "payments"
    assert deployments["items"][0]["revision"]
    for path, body in (("/metrics/pool", pool), ("/deployments", deployments)):
        key = "read-" + path.rsplit("/", 1)[1]
        assert (await client.get(path, headers=headers(key), params=params)).json() == body
        receipt = (await client.get(f"/executions/{key}")).json()
        assert receipt["result"]["output"] == body
        conflict = await client.get(path, headers=headers(key), params={"service": "other"})
        assert conflict.status_code == 409


async def test_health_shape_drives_the_backend_action_and_verified_result(
    client, receiver, monkeypatch
):
    transport = httpx.ASGITransport(app=receiver)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    for run_id, expected in enumerate((False, True), start=1):
        await client.post("/toggle", json={"healthy": expected}, headers=headers(f"set-{run_id}"))
        current = await client.get("/health")
        assert current.status_code == 200
        assert current.json()["healthy"] is expected
        result = await HealthCheck().execute(
            {"url": "http://counterparty-service/health"}, SimpleNamespace(run_id=run_id)
        )
        assert result.ok is expected and result.resolves_alert is expected
        assert (await network.check_execution(run_id))["result"] == result.model_dump()


async def test_runbook_verifies_an_action_after_losing_the_reply(receiver, monkeypatch):
    class LoseFirstReply(httpx.ASGITransport):
        lost = False

        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            if request.method == "POST" and not self.lost:
                self.lost = True
                raise httpx.ReadTimeout("reply lost after commit", request=request)
            return response

    transport = LoseFirstReply(app=receiver)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    monkeypatch.setenv("MOCK_OPS_URL", "http://counterparty-service:8200")
    assert await network.check_execution(41) is None
    context = SimpleNamespace(run_id=41)
    ticket = Ticket()
    with pytest.raises(httpx.ReadTimeout):
        await ticket.execute({"title": "Check pool"}, context)
    receipt = await network.check_execution(41)
    assert receipt["state"] == "succeeded"
    replay = await ticket.execute({"title": "Check pool"}, context)
    assert replay.output["ticket_id"]
    assert replay.model_dump() == receipt["result"]
