import asyncio
import json
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from alerting.contracts import RenderedMessage
from alerting.delivery.providers.telecom import Telecom


NUMBER = "+1-202-555-0100"
NOTICE = {"to": NUMBER, "text": "Settlement TIMEOUT"}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    monkeypatch.setenv("FAKE_TELECOM_DB", str(tmp_path / "receipts.sqlite3"))
    monkeypatch.setenv("ALERTING_API_URL", "http://alert-api.test")
    monkeypatch.setenv("INBOUND_SECRET", "test-provider-secret")
    from mocks.fake_telecom.app import create_app

    return create_app


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://phone.test"
        ) as client:
            yield client


async def test_yi_sender_and_concurrent_retries_survive_restart(gateway):
    msg = RenderedMessage(title="Settlement", body="detail", short=NOTICE["text"], meta={"gen_sent_at": 1234})
    async with client_for(gateway()) as client:
        sms = Telecom("sms", client)
        results = await asyncio.gather(*(sms.send(msg, NUMBER, "sms-01") for _ in range(12)))
        assert all(r.outcome == "delivered" and r.provider_ref == "sms-01" for r in results)
        assert (await Telecom("phone", client).send(msg, NUMBER, "call-01")).outcome == "delivered"
        receipts = (await client.get("/messages", params={"to": NUMBER})).json()["items"]
        assert [(r["idempotency_key"], r["channel"]) for r in receipts] == [
            ("call-01", "phone"), ("sms-01", "sms")
        ]
        assert all(r["gen_sent_at"] == 1234 and r["received_at"] > 1234 for r in receipts)

    async with client_for(gateway()) as client:
        again = await client.post("/sms", json=NOTICE, headers={"Idempotency-Key": "sms-01"})
        assert again.headers["X-Message-Id"] == "sms-01"
        changed = await client.post("/sms", json={**NOTICE, "text": "Different"}, headers={"Idempotency-Key": "sms-01"})
        assert changed.status_code == 409
        assert (await client.get("/messages")).json()["items"] == receipts
        assert (await client.get("/numbers")).json()["items"] == [NUMBER]
        assert (await client.get("/messages", params={"to": "other"})).json()["items"] == []
        assert (await client.post("/sms", json=NOTICE)).status_code == 422


async def test_unavailable_modes_do_not_deliver_and_can_be_switched_back(gateway):
    async with client_for(gateway()) as client:
        for mode in ("down", "503"):
            assert (await client.post("/mode", json={"mode": mode})).json() == {"mode": mode}
            response = await client.post("/call", json=NOTICE, headers={"Idempotency-Key": "call-02"})
            assert response.status_code == 503
            if mode == "503":
                assert int(response.headers["Retry-After"]) > 0
            assert (await client.get("/messages")).json()["items"] == []
        assert (await client.post("/mode", json={"mode": "typo"})).status_code == 422
        assert (await client.get("/mode")).json() == {"mode": "503"}
        await client.post("/mode", json={"mode": "ok"})
        assert (await client.post("/call", json=NOTICE, headers={"Idempotency-Key": "call-02"})).is_success
        assert (await client.get("/messages")).json()["items"][0]["gen_sent_at"] is None


async def test_slow_and_lost_ack_over_tcp(gateway):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(gateway(), log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", trust_env=False) as client:
                for mode in ("slow", "drop_response"):
                    await client.post("/mode", json={"mode": mode})
                    headers = {"Idempotency-Key": mode}
                    with pytest.raises(httpx.ReadTimeout):
                        await client.post("/sms", json=NOTICE, headers=headers, timeout=0.25)
                    await client.post("/mode", json={"mode": "ok"})
                    assert (await client.post("/sms", json=NOTICE, headers=headers)).headers["X-Message-Id"] == mode
                assert len((await client.get("/messages")).json()["items"]) == 2
        finally:
            server.should_exit = True
            await task


async def test_phone_reply_forwards_sender_and_server_secret(gateway, monkeypatch):
    forwarded = []

    def backend(req):
        assert req.method == "POST"
        assert str(req.url) == "http://alert-api.test/v1/inbound/sms"
        assert req.headers["X-Provider-Secret"] == "test-provider-secret"
        forwarded.append(json.loads(req.content))
        return httpx.Response(200) if len(forwarded) == 1 else httpx.Response(409, json={"detail": "Approval expired"})

    transport = httpx.MockTransport(backend)
    reply = {"from": NUMBER, "body": "YES 1234"}
    async with client_for(gateway(inbound_transport=transport)) as client:
        page = await client.get("/")
        assert page.is_success and "test-provider-secret" not in page.text
        assert (await client.post("/reply", json=reply)).status_code == 404
        await client.post("/sms", json=NOTICE, headers={"Idempotency-Key": "reply-01"})
        assert (await client.post("/reply", json=reply, headers={"X-Provider-Secret": "browser-input"})).is_success
        refused = await client.post("/reply", json=reply)
        assert refused.status_code == 409 and refused.json()["detail"] == "Approval expired"
    monkeypatch.delenv("INBOUND_SECRET")
    async with client_for(gateway(inbound_transport=transport)) as client:
        assert (await client.post("/reply", json=reply)).status_code == 503
    assert forwarded == [reply, reply]


async def test_settlement_delivery_and_reply_reach_yis_backend(gateway, inbox_pool, monkeypatch):
    from alerting.api.app import app as alert_api
    from alerting.config import files
    from alerting.delivery.worker import deliver_one
    from alerting.ingest.inputs import prepare
    from alerting.ingest.store import save_batch
    from alerting.rules.storage import Settings, close_settings, get_db
    from alerting.rules.worker import process_batch

    root = Path(__file__).resolve().parents[3]
    source = next(s for s in files.sources() if s["id"] == "workflow-bus")
    sample = (root / "fixtures/events/workflow_failed.json").read_bytes()
    event = prepare(sample, source, files.schemas(), message_id="phone-smoke", headers={"x-gen-sent-at": 1234})
    async with inbox_pool.connection() as conn:
        await save_batch(conn, [event])
    settings = Settings.load()
    assert await process_batch(inbox_pool, settings, lane="normal") == 1
    sam = next(u for u in settings.directory["users"] if u["id"] == "sam")
    monkeypatch.setitem(alert_api.dependency_overrides, get_db, lambda: inbox_pool)
    upstream = httpx.ASGITransport(app=alert_api)
    try:
        async with client_for(gateway(inbound_transport=upstream)) as phone, httpx.AsyncClient(
            transport=upstream, base_url="http://alert-api.test"
        ) as api:
            providers = {channel: Telecom(channel, phone) for channel in ("sms", "phone")}
            for channel in providers:
                assert await deliver_one(inbox_pool, providers, [channel])
            messages = (await phone.get("/messages")).json()["items"]
            assert {(m["channel"], m["to"], m["gen_sent_at"]) for m in messages} == {
                (ch, sam["contacts"][ch], 1234) for ch in providers
            }
            alert = (await api.get("/v1/alerts")).json()["items"][0]
            action = await api.post(
                f"/v1/alerts/{alert['id']}/actions", json={"step_index": 2}, headers={"X-User": "sam"}
            )
            assert action.json()["state"] == "pending_approval"
            assert await deliver_one(inbox_pool, providers, ["sms"])
            approval = (await phone.get("/messages", params={"limit": 1})).json()["items"][0]
            decline = re.search(r"\bNO \d{4}\b", approval["text"]).group()
            reply = await phone.post("/reply", json={"from": approval["to"], "body": decline})
            assert reply.is_success, reply.text
            async with inbox_pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT state, approved_by FROM action_runs WHERE id=%s", (action.json()["id"],)
                )).fetchone()
                assert row == ("rejected", "lee")
    finally:
        await close_settings(inbox_pool)
