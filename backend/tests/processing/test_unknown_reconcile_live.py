"""Exercise the actual HTTP timeout and SQLite receipt against a real mock-ops socket."""

import asyncio
import socket
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn

from .test_worker import Settings, process_batch, seed


async def test_real_mock_ops_lost_reply_is_reconciled_without_another_execution(
    api, database, monkeypatch, tmp_path,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    from mocks.mock_ops.service import create_app

    monkeypatch.setenv("DEMO_MODE", "1")
    receipt_db = tmp_path / "mock-ops.sqlite3"
    mock_ops = create_app(receipt_db)
    calls = []

    async def observe(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("/workflows/", "/executions/")):
            calls.append((scope["method"], scope["path"], dict(scope["headers"]).get(b"idempotency-key")))
        await mock_ops(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(observe, log_level="warning", ws="none"))
    await seed(database)
    await process_batch(database, Settings.load())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        monkeypatch.setenv("MOCK_OPS_URL", origin)
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as receiver:
                arm = await receiver.post("/demo/delay-next", json={"action": "retry_workflow"})
                arm.raise_for_status()
            started = await api.post(
                "/v1/alerts/1/actions", headers={"X-User": "sam"}, json={"step_index": 1}
            )
            started.raise_for_status()
            run = started.json()
            assert run["state"] == "unknown"
            action_id = run["id"]
            original_key = f"action-{action_id}"
            retry = await api.post(
                f"/v1/action-runs/{action_id}/retry", headers={"X-User": "sam"},
                json={"confirm": True},
            )
            assert retry.status_code == 409
            reconciled = await api.post(
                f"/v1/action-runs/{action_id}/reconcile", headers={"X-User": "lee"}
            )
            reconciled.raise_for_status()
            assert reconciled.json()["state"] == "succeeded"
            assert reconciled.json()["result"]["output"]["attempt"] == 1
            post, lookup = calls
            assert post[0] == "POST" and post[2] == original_key.encode()
            assert lookup == ("GET", f"/executions/{original_key}", None)
            with closing(sqlite3.connect(receipt_db)) as conn:
                assert conn.execute("select attempts from workflows").fetchall() == [(1,)]
                assert conn.execute("select key from receipts").fetchall() == [(original_key,)]
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 8)

    async with database.connection() as conn:
        cur = await conn.execute(
            """select actor, action, detail->>'receiver_state' from audit_log
               where detail->>'run_id'=%s and action in ('action.request','action.execute','action.reconcile')
               order by id""",
            (str(action_id),),
        )
        assert await cur.fetchall() == [
            ("sam", "action.request", None),
            ("sam", "action.execute", None),
            ("lee", "action.reconcile", "succeeded"),
        ]
