"""Read the PostgreSQL messages emitted by real ingest, rules, outbox and runbook transactions."""

import json

import httpx
import pytest
from alerting import db
from alerting.api.app import app
from alerting.config import files
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.rules.storage import close_settings, get_db
from alerting.rules.worker import process_batch
from psycopg_pool import AsyncConnectionPool

from .test_golden import GOLD, ROOT


@pytest.mark.e2e
async def test_trace_connects_exact_event_after_batching_and_retry(api, database, monkeypatch):
    messages = []

    def capture(diagnostic):
        message = diagnostic.message_primary
        if message and message.startswith("alert_funnel "):
            messages.append(json.loads(message.removeprefix("alert_funnel ")))

    async def configure(conn):
        conn.add_notice_handler(capture)
        await conn.execute("set client_min_messages = log")
        await conn.commit()

    case = next(c for c in GOLD["cases"] if c["id"] == "settlements-positive")
    raw = json.loads((ROOT / case["send"]["file"]).read_text())
    source = next(
        s for s in files.sources() if s["transport"] == "http" and not s.get("split_path")
    )
    async with AsyncConnectionPool(
        database.conninfo, configure=configure, min_size=1, open=False
    ) as pool:
        for dependency in (db.pool, get_db):
            monkeypatch.setitem(app.dependency_overrides, dependency, lambda: pool)
        try:
            ids = []
            for occurrence in (1, 2):
                response = await api.post(
                    "/v1/events/" + source["id"],
                    headers={"X-Api-Key": source["api_key"]},
                    json={
                        **raw,
                        "workflowId": f"trace-{occurrence}",
                        "private_marker": "do-not-log-this",
                    },
                )
                assert response.status_code == 202, response.text
                ids.extend(response.json()["event_ids"])
            assert await process_batch(pool, batch=2) == 2
            async with pool.connection() as conn:
                aid, representative = await (
                    await conn.execute("select id,representative_event_id from alerts")
                ).fetchone()
            assert representative == ids[1]
            with pytest.raises(RuntimeError):
                async with pool.connection() as conn:
                    (aborted,) = await (
                        await conn.execute(
                            "insert into events(source_id,transport,payload_hash,raw) "
                            "values ('rollback','http','rollback','{}') returning id"
                        )
                    ).fetchone()
                    raise RuntimeError("test rollback")

            answers = iter((503, 200))
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(next(answers)))
            ) as client:
                senders = {"sms": Telecom("sms", client)}
                assert await deliver_one(pool, senders, ["sms"])
                async with pool.connection() as conn:
                    await conn.execute(
                        "update notifications set next_attempt_at=now() where state='retrying'"
                    )
                assert await deliver_one(pool, senders, ["sms"])
            async with pool.connection() as conn:
                (nid,) = await (
                    await conn.execute(
                        "select id from notifications where channel='sms' and kind='initial'"
                    )
                ).fetchone()
            directory = files.directory()
            team = next(t for t in directory["teams"] if t["id"] == case["expect"]["alert"]["team"])
            path = f"/v1/alerts/{aid}"
            steps = (await api.get(path + "/runbook")).json()["steps"]
            step = next(s for s in steps if s.get("approval") == "required")
            response = await api.post(
                path + "/actions",
                json={"step_index": step["index"]},
                headers={"X-User": team["members"][0]},
            )
            assert response.status_code == 200, response.text
            run_id = response.json()["id"]
            response = await api.post(
                f"/v1/action-runs/{run_id}/reject", headers={"X-User": team["approvers"][0]}
            )
            assert response.status_code == 200, response.text
        finally:
            await close_settings(pool)

    assert set(ids) == {m["event_id"] for m in messages if m["stage"] == "ingest"}
    assert not any(m["event_id"] == aborted for m in messages)
    assert all(
        {"stage", "event_id", "alert_id", "notification_id", "run_id"} <= m.keys() for m in messages
    )
    root = [m for m in messages if m["event_id"] == ids[0]]
    assert {"ingest", "rules", "outbox", "delivery", "runbook"} <= {m["stage"] for m in root}
    attempts = [
        (m["attempt"], m["state"])
        for m in root
        if m["stage"] == "delivery" and m["notification_id"] == str(nid)
    ]
    assert attempts == [(1, "sending"), (1, "retryable"), (2, "sending"), (2, "delivered")]
    assert {m["state"] for m in root if m["stage"] == "runbook" and m["run_id"] == run_id} == {
        "pending_approval",
        "rejected",
    }
    assert any(
        m["stage"] == "outbox"
        and m["alert_id"] == aid
        and m["notification_id"]
        and m["run_id"] == run_id
        for m in root
    )
    assert "do-not-log-this" not in json.dumps(messages)


@pytest.mark.e2e
async def test_fallback_uses_original_routing_event_for_old_untagged_rows(database):
    from .test_worker import seed

    await seed(database, 2)
    assert await process_batch(database, batch=2) == 2
    async with database.connection() as conn:
        rows = await (
            await conn.execute(
                "select event_id,alert_id from decisions where alert_id is not null order by event_id"
            )
        ).fetchall()
        first, second = rows[0], rows[-1]
        assert first[0] != second[0] and first[1] == second[1]
        await conn.execute(
            """update decisions set reason = reason || '{"notifications":["old-update"]}'::jsonb
               where event_id=%s and alert_id=%s""",
            second,
        )
        add = """insert into notifications(alert_id,kind,recipient,channel,rendered,dedup_key,fallback_of)
                 values (%s,'update','test','ui','{}',%s,%s) returning id"""
        parent = None
        for key in ("old-update", "old-fallback"):
            (parent,) = await (await conn.execute(add, (first[1], key, parent))).fetchone()
    async with database.connection() as conn:
        # Existing volumes contain outbox rows created before trace columns existed.
        await conn.execute("update notifications set trace_event_id=null,trace_run_id=null")
        (child,) = await (await conn.execute(add, (first[1], "new-fallback", parent))).fetchone()
    async with database.connection() as conn:
        assert await (
            await conn.execute("select trace_event_id from notifications where id=%s", (child,))
        ).fetchone() == (second[0],)
