import json
from pathlib import Path

import pytest


async def store(client, body, source="generic-webhook"):
    response = await client.post(f"/v1/events/{source}", json=body, headers={"X-Api-Key": f"demo-{source.removesuffix('-webhook')}-key"})
    assert response.status_code == 202
    return response.json()["event_ids"]


async def page(client, **filters):
    response = await client.get("/v1/events", params=filters)
    assert response.status_code == 200
    return response.json()


async def test_events_query_filters_known_and_unseen_schemas(client):
    sample = Path(__file__).resolve().parents[3] / "fixtures/events/alertmanager_high_error_rate.json"
    known_id, = await store(client, json.loads(sample.read_text(encoding="utf-8")), "alertmanager")
    workflow = sample.with_name("workflow_failed.json")
    workflow_id, = await store(client, json.loads(workflow.read_text(encoding="utf-8")))
    unknown_id, = await store(client, {"sensor": {"status": "HOT", "reading": 42}})

    match = await page(client, source="generic-webhook", schema="WORKFLOW_FAILED", status="pending",
                       field="error.code", value="TIMEOUT")
    event, = match["items"]
    assert event == (await client.get(f"/v1/events/{workflow_id}")).json()
    assert event["fields"]["$schema"] == "WORKFLOW_FAILED"
    assert [row["id"] for row in (await page(client, schema="workflow"))["items"]] == [workflow_id]
    assert [row["id"] for row in (await page(client, source="alertmanager"))["items"]] == [known_id]

    review = await page(client, status="needs_review", field="sensor.reading", value="42")
    event, = review["items"]
    assert event["id"] == unknown_id and event["raw"]["sensor"]["reading"] == event["fields"]["sensor.reading"] == 42


async def test_events_query_typed_equality_and_literal_flattened_paths(client):
    quoted_path = "sensor's status?"
    numeric, string, missing, null = await store(client, [
        {"data": {"v": 12}, "levels": [1, 2], "attrs": {"on": True, "max": 8}},
        {"data": {"v": "12"}, "levels": [2, 1, 1], "attrs": {"on": True}},
        {"data": {}, "levels": [1], quoted_path: "room's fan %"},
        {"data": {"v": None}},
    ])

    async def ids(field, **other):
        return [row["id"] for row in (await page(client, field=field, **other))["items"]]

    assert await ids("data.v", value="12") == [numeric]
    assert await ids("data.v", value='"12"') == [string]
    assert await ids("data.v", value="null") == [null]
    assert await ids("data.v") == [null, string, numeric]
    assert await ids("attrs", value='{"on":true}') == [string]
    assert await ids("attrs.on", value="true") == [string, numeric]
    assert await ids("levels", value="[1,2]") == [numeric]
    assert await ids("levels.0", value="1") == [missing, numeric]
    assert await ids(quoted_path, value="room's fan %") == [missing]
    assert await ids("data.v", value="room's fan %") == []


async def test_events_query_descending_keyset_pages_ignore_newer_appends(client):
    first, second, third = await store(client, [{"sample": index} for index in range(3)])
    latest = await page(client, limit=2)
    assert [row["id"] for row in latest["items"]] == [third, second]
    assert latest["next_cursor"] == second

    await store(client, {"sample": 4})
    older = await page(client, limit=2, cursor=latest["next_cursor"])
    assert [row["id"] for row in older["items"]] == [first]
    assert older["next_cursor"] is None


async def test_events_query_supports_golden_runner_offset_pages(client):
    event_ids = await store(client, [{"sample": index} for index in range(205)])
    ordered = list(reversed(event_ids))
    for offset in (0, 100, 200, 205):
        response = await page(client, source="generic-webhook", limit=100, offset=offset)
        assert [row["id"] for row in response["items"]] == ordered[offset:offset + 100]


@pytest.mark.parametrize("params", [
    {"value": "missing-field"},
    {"field": "\0"},
    {"field": "val", "value": r'"\u0000"'},
    {"field": "val", "value": r'"\ud800"'},
    {"field": "val", "value": "[" * 25 + "0" + "]" * 25},
    {"field": "val", "value": "1e9999"},
    {"field": "val", "value": "x" * 8193},
    {"status": "invalid"},
    {"limit": 101},
    {"cursor": -1},
    {"cursor": 2**63},
    {"offset": -1},
    {"offset": 100_001},
    {"cursor": 42, "offset": 0},
])
async def test_events_query_rejects_invalid_or_unbounded_filters(client, params):
    assert (await client.get("/v1/events", params=params)).status_code == 422
