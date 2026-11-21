import asyncio

import httpx
import pytest

from mocks.mock_ops import service


@pytest.mark.parametrize("mode", [None, "0", "true"])
async def test_delay_control_requires_explicit_demo_mode(tmp_path, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("DEMO_MODE", raising=False)
    else:
        monkeypatch.setenv("DEMO_MODE", mode)
    app = service.create_app(tmp_path / "ops.sqlite3")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://mock-ops") as client:
        response = await client.post("/demo/delay-next", json={"action": "retry_workflow"})
        assert response.status_code == 404


@pytest.mark.parametrize(
    ("action", "path"),
    [
        ("retry_workflow", "/workflows/synthetic/retry"),
        ("restart_service", "/services/synthetic/restart"),
        ("open_ticket", "/tickets"),
    ],
)
async def test_receipt_and_replay_are_available_while_only_the_fresh_reply_waits(
    tmp_path, monkeypatch, action, path
):
    monkeypatch.setenv("DEMO_MODE", "1")
    entered, release = asyncio.Event(), asyncio.Event()
    delays = []

    async def wait_for_release(seconds):
        delays.append(seconds)
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "sleep", wait_for_release)
    app = service.create_app(tmp_path / "ops.sqlite3")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://mock-ops") as client:
        def perform(url, key):
            return client.post(url, json={"title": "Synthetic"}, headers={"Idempotency-Key": key})

        original = await perform(path, "already-recorded")
        assert original.is_success
        assert (await client.post("/demo/delay-next", json={"action": "unsupported"})).status_code == 422
        assert (await client.post("/demo/delay-next", json={"action": action, "seconds": 10})).status_code == 422
        assert (await client.post("/demo/delay-next", json={"action": action})).is_success
        assert (await client.post("/demo/delay-next", json={"action": action})).is_success
        async with asyncio.timeout(2):
            assert (await perform(path, "already-recorded")).json() == original.json()
            unrelated = "/tickets" if action != "open_ticket" else "/services/other/restart"
            assert (await perform(unrelated, "unrelated")).is_success
            assert (await client.get("/health")).is_success
        first = asyncio.create_task(perform(path, "action-42"))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
                receipt = (await client.get("/executions/action-42")).raise_for_status().json()
                assert receipt["state"] == "succeeded"
                replay = await perform(path, "action-42")
                assert replay.json() == receipt["result"]["output"]
                assert (await perform(path, "action-43")).is_success
                assert not first.done()
        finally:
            release.set()
            reply = await first
        assert reply.json() == replay.json()
        assert len(delays) == 1 and delays[0] > 3
