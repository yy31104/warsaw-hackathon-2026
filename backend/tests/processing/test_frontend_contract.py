import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.rules.storage import emit
from .notify import listen
from .test_worker import seed, process_batch

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "api"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


async def test_alert_and_runbook_shapes(api, database):
    await seed(database)
    await process_batch(database)
    feed = (await api.get("/v1/alerts")).json()
    example = fixture("alerts")
    assert feed.keys() == example.keys() and feed["total"] == 1
    (item,) = feed["items"]
    assert item.keys() == example["items"][0].keys()
    assert isinstance(item["score"], (int, float))
    assert item["score_breakdown"].keys() == example["items"][0]["score_breakdown"].keys()
    path = f"/v1/alerts/{item['id']}"
    detail = (await api.get(path)).json()
    model = fixture("alert_detail")
    assert detail.keys() == model.keys() | {"representative_event_id"}
    for key in ("occurrences", "timeline"):
        assert detail[key] and all(row.keys() == model[key][0].keys() for row in detail[key])
    book = (await api.get(path + "/runbook")).json()
    model = fixture("runbook")
    assert book.keys() == model.keys()
    for actual, expected in zip(book["steps"], model["steps"], strict=True):
        assert actual.keys() == expected.keys()
        assert actual["params"].keys() == expected["params"].keys()
    for transition in ("ack", "resolve"):
        updated = (await api.post(path + "/" + transition, headers={"X-User": "sam"})).json()
        assert updated.keys() == item.keys()
    explanation = (await api.get("/v1/events/1/explain")).json()
    assert explanation["id"] == 1 and explanation["decisions"] and explanation["notifications"]


async def test_alert_detail_explains_representative_event(api, database):
    # Process the lower-ID occurrence last, so the final sample differs from the representative.
    await seed(database, lane="critical")
    await seed(database)
    assert await process_batch(database) == 1
    assert await process_batch(database, lane="critical") == 1

    (item,) = (await api.get("/v1/alerts")).json()["items"]
    response = await api.get(f"/v1/alerts/{item['id']}")
    assert response.status_code == 200
    detail = response.json()
    first, _ = detail["occurrences"]
    event_id = detail["representative_event_id"]
    assert event_id == first["event_id"]

    response = await api.get(f"/v1/events/{event_id}/explain")
    assert response.status_code == 200
    explanation = response.json()
    assert explanation["id"] == event_id
    (decision,) = [row for row in explanation["decisions"] if row["alert_id"] == detail["id"]]
    assert decision["rule_id"] == detail["rule_id"]
    assert decision["rule_version"] == detail["rule_version"]
    assert decision["outcome"] == "SUPPRESSED_COOLDOWN"
    assert decision["reason"]["matched"] and decision["reason"]["trace"]


async def test_notification_shapes(api, database):
    await seed(database)
    await process_batch(database)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        assert await deliver_one(database, {"sms": Telecom("sms", client)}, ["sms"])
    example = fixture("notifications")
    for path in ("/v1/notifications", "/v1/alerts/1/notifications"):
        result = (await api.get(path)).json()
        assert result.keys() == example.keys()
        assert len(result["items"]) == 3
        assert all(row.keys() == example["items"][0].keys() for row in result["items"])
        (attempt,) = next(n for n in result["items"] if n["channel"] == "sms")["attempt_log"]
        assert attempt.keys() == example["items"][0]["attempt_log"][0].keys()
        assert attempt["outcome"] == "delivered" and isinstance(attempt["latency_ms"], int)


async def test_committed_event_envelopes(api, database):
    async with listen(database) as drain:
        await seed(database)
        await process_batch(database)
        await api.post("/v1/alerts/1/ack", headers={"X-User": "sam"})
        run = (
            await api.post(
                "/v1/alerts/1/actions", json={"step_index": 2}, headers={"X-User": "sam"}
            )
        ).json()
        assert (
            await api.post(f"/v1/action-runs/{run['id']}/reject", headers={"X-User": "lee"})
        ).status_code == 200
        signals = await drain()
    expected = {
        "alert.created": "alerts",
        "alert.updated": "alerts",
        "notification.updated": "notifications",
        "action.updated": "actions",
    }
    assert {message["type"] for _, message in signals} == set(expected)
    for channel, message in signals:
        assert set(message) == {"type", "data"} and channel == expected[message["type"]]
        data = message["data"]
        assert data["id"] and data["team_id"] == "settlements"
        assert datetime.fromisoformat(data["ts"]).tzinfo is not None


async def test_emit_rejects_misrouted_events():
    with pytest.raises(ValueError):
        await emit(None, "actions", "notification.updated", id=1, team_id="example")
    with pytest.raises(ValueError):
        await emit(None, "config", "config.changed", id=1, team_id="example")
    with pytest.raises(TypeError):
        await emit(None, "alerts", "alert.updated", id=1)
