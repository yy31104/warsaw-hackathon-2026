"""Actual HTTP ingest, rules, outbox and delivery with a temporary PostgreSQL schema."""
import json
from pathlib import Path
from shutil import copytree

import httpx
import pytest
import yaml
from fastapi import FastAPI

from alerting import db
from alerting.api.routes_ingest import router
from alerting.canary.monitor import CANARY_NUMBER, OPS_NUMBER, Monitor, Settings
from alerting.clock import SYSTEM
from alerting.config import files
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.rules.worker import process_batch
from tests.processing.conftest import database  # noqa: F401 -- shared random-schema fixture

EXAMPLE = Path(__file__).resolve().parents[2] / "alerting/canary/config.example.yaml"


async def test_two_probes_single_alert_then_stop_delivery(database, tmp_path, monkeypatch):
    config = tmp_path / "config"
    copytree(files.CONFIG_DIR, config)
    example = yaml.safe_load(EXAMPLE.read_text())
    for section, entries in example.items():
        file = config / ("teams.yaml" if section == "users" else f"{section}.yaml")
        doc = yaml.safe_load(file.read_text())
        doc[section] = entries + doc[section]
        file.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(files, "CONFIG_DIR", config)

    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[db.pool] = lambda: database
    ingest = httpx.ASGITransport(app=api)
    receipts = []
    worker_running = True

    async def gateway(request):
        if request.url.path == "/sms":
            sms = json.loads(request.content)
            receipts.append(dict(sms, channel="sms", received_at=int(SYSTEM.now().timestamp() * 1000),
                                 gen_sent_at=int(request.headers.get("X-Gen-Sent-At", 0))))
            return httpx.Response(200)
        return httpx.Response(200, json={"items": receipts.copy()})

    telecom = httpx.MockTransport(gateway)
    monkeypatch.setenv("FAKE_TELECOM_URL", "http://telecom")
    async with httpx.AsyncClient(transport=telecom) as receiver:
        async def dispatch(request):
            if request.url.host == "api":
                response = await ingest.handle_async_request(request)
                assert response.status_code == 202
                assert await process_batch(database) == 1
                if worker_running:
                    assert await deliver_one(database, {"sms": Telecom("sms", receiver)}, ["sms"])
                return response
            return await telecom.handle_async_request(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as client:
            monitor = Monitor(client, Settings("http://api", "http://telecom", "demo-canary-key", 2))
            for _ in range(2):
                await monitor.probe()
                assert monitor.last_probe["outcome"] == "delivered"
            worker_running = False
            monitor = Monitor(client, Settings("http://api", "http://telecom", "demo-canary-key", .4))
            await monitor.probe()
            assert monitor.last_probe["outcome"] == "missed" and monitor.last_probe["bypass"] == "sent"

    assert [r["to"] for r in receipts] == [CANARY_NUMBER, CANARY_NUMBER, OPS_NUMBER]
    async with database.connection() as conn:
        assert await (await conn.execute("select distinct trace->'synthetic' from events")).fetchall() == [(True,)]
        alerts = await (await conn.execute("select occurrence_count, next_notify_at from alerts")).fetchall()
        rows = await (await conn.execute("select kind, recipient, state from notifications order by created_at")).fetchall()
    assert alerts == [(3, None)]
    assert rows == [("canary", "canary", s) for s in ("delivered", "delivered", "pending")]


class UnscopedCanary(AssertionError):
    """An ordinary rule handled a reserved synthetic probe."""


@pytest.mark.xfail(raises=UnscopedCanary, reason="W0/W1 integration: apply canary.policy.allows_rule")
async def test_catchall_rule_only_sees_real_events_in_a_mixed_batch(database):
    from alerting.ingest.inputs import prepare
    from alerting.ingest.store import save_batch
    from alerting.rules.engine import Ruleset
    from alerting.rules.storage import Settings as WorkerSettings

    snippets = yaml.safe_load(EXAMPLE.read_text())
    directory = {"users": snippets["users"] + [{"id": "operator", "contacts": {"sms": "+15550100555"}}],
                 "teams": snippets["teams"], "default_preferences": []}
    catchall = {"id": "catchall", "owner": "team:canary", "match": {"all": []},
                "route": [{"to": "user:operator", "channels": ["sms"]}]}
    settings = WorkerSettings(Ruleset([*snippets["rules"], catchall], snippets["schemas"]), directory, [])
    event = prepare({"probe_id": "synthetic-only"}, snippets["sources"][0], snippets["schemas"])
    other_source = {"id": "ordinary-source", "transport": "http"}
    other_schema = {"id": "ordinary", "name": "other", "detect": {"field": "ordinary", "op": "exists"}}
    real = prepare({"ordinary": "job failed"}, other_source, [other_schema])
    async with database.connection() as conn:
        await save_batch(conn, [real, event])
    await process_batch(database, settings=settings)
    async with database.connection() as conn:
        targets = await (await conn.execute("select recipient from notifications where state='pending'")).fetchall()
        alerts = await (await conn.execute("select rule_id,occurrence_count from alerts order by rule_id")).fetchall()
        assert await (await conn.execute("select source_id,trace->'synthetic' from events order by id")).fetchall() == [
            ("ordinary-source", None), ("canary", True)]
        cross = await (await conn.execute(
            "select d.alert_id from decisions d join events e on e.id=d.event_id "
            "where d.rule_id='catchall' and e.source_id='canary' and d.alert_id is not null")).fetchone()
        leak = await (await conn.execute(
            "select n.recipient, n.channel from decisions d join events e on e.id=d.event_id "
            "join notifications n on n.alert_id=d.alert_id and d.reason->'notifications' ? n.dedup_key "
            "where e.source_id='canary' and n.recipient='operator' and n.state<>'cancelled'")).fetchone()
    if leak:
        raise UnscopedCanary(f"The native worker queued a probe notification to {leak[0]} via {leak[1]}")
    assert cross is None
    assert sorted(targets) == [("canary",), ("operator",)]
    assert alerts == [("catchall", 1), ("delivery-canary", 1)]


async def test_reserved_rule_cancels_misconfigured_contact(database):
    from alerting.ingest.inputs import prepare
    from alerting.ingest.store import save_batch
    from alerting.rules.engine import Ruleset
    from alerting.rules.storage import Settings as WorkerSettings

    snippet = yaml.safe_load(EXAMPLE.read_text())
    snippet["users"][0]["contacts"]["sms"] = "+15550100555"
    settings = WorkerSettings(Ruleset(snippet["rules"], snippet["schemas"]), snippet, [])
    event = prepare({"probe_id": "misrouted"}, snippet["sources"][0], snippet["schemas"])
    async with database.connection() as conn:
        await save_batch(conn, [event])
    await process_batch(database, settings)
    async with database.connection() as conn:
        row = await (await conn.execute("select kind,state,attempts from notifications")).fetchone()
    assert row == ("canary", "cancelled", 0)

@pytest.mark.parametrize("is_probe", [False, True])
async def test_proposed_guard_routes_match_all_only_for_real_input(database, is_probe):
    from alerting.canary.policy import allows_rule
    from alerting.ingest.inputs import prepare
    from alerting.ingest.store import save_batch
    from alerting.rules.engine import Ruleset
    from alerting.rules.storage import Settings as WorkerSettings

    snippet = yaml.safe_load(EXAMPLE.read_text())
    human = {"id": "human", "contacts": {"sms": "+15550100555"}}
    directory = dict(snippet, users=[*snippet["users"], human])
    catchall = {"id": "match-everything", "owner": "team:canary", "match": {"all": []},
                "route": [{"to": "user:human", "channels": ["sms"]}]}
    source = snippet["sources"][0] if is_probe else {"id": "ordinary", "transport": "http"}
    ordinary = {"id": "other", "name": "ordinary", "detect": {"field": "$source", "op": "eq", "value": "ordinary"}}
    schemas = [*snippet["schemas"], ordinary]
    event = prepare({"probe_id": "example", "trace": {"synthetic": True}}, source, schemas)
    async with database.connection() as conn:
        await save_batch(conn, [event])
        trace, = await (await conn.execute("select trace from events")).fetchone()
    stored_event = event.model_copy(update={"trace": trace})
    allowed = [rule for rule in [*snippet["rules"], catchall] if allows_rule(stored_event, rule)]
    settings = WorkerSettings(Ruleset(allowed, schemas), directory, [])
    await process_batch(database, settings)
    async with database.connection() as conn:
        recipients = await (await conn.execute("select recipient, state from notifications")).fetchall()
    assert recipients == [("canary" if is_probe else "human", "pending")]
    received = []
    def gateway(request):
        received.append(json.loads(request.content)["to"])
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        assert await deliver_one(database, {"sms": Telecom("sms", client)}, ["sms"])
    assert received == [CANARY_NUMBER if is_probe else human["contacts"]["sms"]]
