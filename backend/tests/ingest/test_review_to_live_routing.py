"""An unseen producer's verification, historical replay and live inbox must agree."""
from alerting.api.app import app
from alerting.rules.storage import close_settings, get_db
from alerting.rules.worker import process_batch


async def test_cent_amounts_route_identically_before_and_after_approval(client, inbox_pool, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: inbox_pool)
    monkeypatch.setenv("PUBLIC_UI_URL", "http://127.0.0.1:18173/")
    merchant = {"desk": "rates", "currency": "EUR", "reason": "desk limit"}
    events = [
        {"newEnvelope": "v1", "payload": {**merchant, "tradeId": "large", "notional": 2_500_000_000}},
        {"newEnvelope": "v1", "payload": {**merchant, "tradeId": "small", "notional": 25_000_000}},
    ]
    mapping = {
        "id": "new-cents-envelope", "detect": {"field": "newEnvelope", "op": "eq", "value": "v1"},
        "name": "trade.rejected.cents", "severity_default": "warning", "event_id": "{{payload.tradeId}}",
        "transforms": {"payload.notional": {"scale": 0.01, "unit": "EUR"}},
    }
    api_key = {"X-Api-Key": "demo-generic-key"}
    async def ingest(batch):
        result = await client.post("/v1/events/generic-webhook", json=batch, headers=api_key)
        result.raise_for_status()
        return result.json()["event_ids"]

    async def get_event(event_id):
        result = await client.get(f"/v1/events/{event_id}")
        result.raise_for_status()
        return result.json()

    try:
        old_ids = await ingest(events)
        assert len(old_ids) == 2
        first = await get_event(old_ids[0])
        assert first["status"] == "needs_review"
        expected = {"recipients": ["rita", "team:rates"], "channels": ["email", "ui"]}
        result = await client.post("/v1/schemas/verify", json={
            "schema": mapping, "samples": [
                {"event": events[0], "expect": expected},
                {"event": events[1], "expect": "quiet"},
            ],
        })
        result.raise_for_status()
        verification = result.json()
        assert verification["all_pass"], verification
        assert "payload.notional" in verification["samples"][0]["deciding_fields"]
        assert (await get_event(old_ids[0]))["status"] == "needs_review"
        published = await client.post("/v1/schemas", json=mapping, headers={"X-User": "ops"},
                                      params={"fingerprint": first["review_fingerprint"]})
        published.raise_for_status()
        assert published.json()["reprocessed"] == 2
        live_ids = await ingest([
            {**event, "payload": {**event["payload"], "tradeId": "live-" + event["payload"]["tradeId"]}}
            for event in events
        ])
        assert await process_batch(inbox_pool) == 4
        for event_id, expected_amount in zip(old_ids + live_ids, [25_000_000, 250_000] * 2, strict=True):
            stored = await get_event(event_id)
            assert (stored["status"], stored["fields"]["payload.notional"]) == ("processed", expected_amount)
        async with inbox_pool.connection() as conn:
            links = await (await conn.execute("""
                select a.id, n.rendered->>'link' from alerts a join notifications n on n.alert_id=a.id
                where a.rule_id='rates-big-rejects'
            """)).fetchall()
            assert len(links) == 4
            assert all(link == f"http://127.0.0.1:18173/a/{alert_id}" for alert_id, link in links)
            rows = await (await conn.execute("""
                select o.event_id, n.recipient, n.channel from notifications n
                join alerts a on a.id=n.alert_id join alert_occurrences o on o.alert_id=a.id
                where a.rule_id='rates-big-rejects' order by o.event_id,n.channel
            """)).fetchall()
        assert rows == [(i, recipient, channel) for i in (old_ids[0], live_ids[0])
                        for recipient, channel in (("rita", "email"), ("team:rates", "ui"))]
    finally:
        await close_settings(inbox_pool)
