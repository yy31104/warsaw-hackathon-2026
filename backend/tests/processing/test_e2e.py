"""Run with uv run pytest -q -m e2e. Only PostgreSQL is external; telecom runs in process."""

import json

import httpx
import pytest
from psycopg.types.json import Jsonb

from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.rules.worker import process_batch
from .notify import listen
from .test_golden import GOLD, ROOT, make_event
from .test_normalizer_contract import assert_workflow_contract


@pytest.mark.e2e
async def test_pending_workflow_to_delivery_and_signals(database):
    case = next(c for c in GOLD["cases"] if c["id"] == "settlements-positive")
    raw = json.loads((ROOT / case["send"]["file"]).read_text())
    event = make_event(raw, "workflow", case["send"]["source"])
    assert_workflow_contract(event, raw)
    received = []

    def gateway(request):
        received.append(
            (request.url.path, request.headers["Idempotency-Key"], json.loads(request.content))
        )
        return httpx.Response(200)

    async with listen(database) as drain:
        async with database.connection() as conn:
            await conn.execute(
                """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,
                schema_name,severity,service,environment,status) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')""",
                (
                    event.source_id,
                    event.transport,
                    event.payload_hash,
                    Jsonb(raw),
                    Jsonb(event.fields),
                    event.schema_id,
                    event.schema_name,
                    event.severity.value,
                    event.service,
                    event.environment,
                ),
            )
        assert await process_batch(database, batch=1) == 1
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
            # One delivery pass on each external channel. The UI notification was delivered on insert.
            for channel in ("phone", "sms"):
                assert await deliver_one(database, {channel: Telecom(channel, client)}, [channel])
        signals = await drain()

    async with database.connection() as conn:
        assert await (
            await conn.execute("select status,raw,fields->>'$schema' from events")
        ).fetchone() == ("processed", raw, event.schema_name)
        alert_id, rule, team, count = await (
            await conn.execute("select id,rule_id,team_id,occurrence_count from alerts")
        ).fetchone()
        assert (rule, team, count) == (
            case["expect"]["alert"]["rule"],
            case["expect"]["alert"]["team"],
            1,
        )
        messages = await (
            await conn.execute("select id,recipient,channel,state,attempts from notifications")
        ).fetchall()
        attempts = await (
            await conn.execute("select notification_id,attempt,outcome from delivery_attempts")
        ).fetchall()
    assert {(u, c) for _, u, c, _, _ in messages} == {
        (n["recipient"], n["channel"]) for n in case["expect"]["notifications"]
    }
    assert all((state, n) == ("delivered", 1) for _, _, _, state, n in messages)
    external = {id_ for id_, _, ch, _, _ in messages if ch != "ui"}
    assert set(attempts) == {(id_, 1, "delivered") for id_ in external}
    assert {path for path, _, _ in received} == {"/call", "/sms"}
    assert {key for _, key, _ in received} == {str(id_) for id_ in external}
    assert all(payload["to"] and payload["text"] for _, _, payload in received)
    assert any(
        ch == "alerts" and msg["type"] == "alert.created" and msg["data"]["id"] == alert_id
        for ch, msg in signals
    )
    delivered = {
        msg["data"]["id"]
        for ch, msg in signals
        if ch == "notifications"
        and msg["type"] == "notification.updated"
        and msg["data"]["state"] == "delivered"
    }
    assert delivered == {str(id_) for id_, *rest in messages}
    assert all(msg["data"]["team_id"] == team and msg["data"]["ts"] for _, msg in signals)
