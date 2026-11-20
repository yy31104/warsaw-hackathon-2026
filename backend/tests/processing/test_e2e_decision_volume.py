"""Measure persisted decisions for 1,000 distinct workflow failures sharing one alert key."""

import json

import pytest
from alerting import db
from alerting.api.app import app
from alerting.config import files
from alerting.rules.worker import process_batch

from .test_golden import ROOT


@pytest.mark.e2e
async def test_decision_rows_per_thousand_hot_key_events(api, database, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, db.pool, lambda: database)
    source = next(
        s
        for s in files.sources()
        if s["transport"] == "http" and not s.get("split_path") and s["id"] != "canary"
    )
    raw = json.loads((ROOT / "fixtures/events/workflow_failed.json").read_text())
    for start in range(0, 1000, 100):
        response = await api.post(
            "/v1/events/" + source["id"],
            headers={"X-Api-Key": source["api_key"]},
            json=[{**raw, "workflowId": f"sample-{n}"} for n in range(start, start + 100)],
        )
        assert response.status_code == 202, response.text
        assert response.json()["accepted"] == 100

    processed = 0
    while count := await process_batch(database, batch=200):
        processed += count
    async with database.connection() as conn:
        counts = dict(
            await (
                await conn.execute(
                    "select outcome,count(*) from decisions group by outcome order by outcome"
                )
            ).fetchall()
        )
        (covered,) = await (
            await conn.execute("select count(distinct event_id) from decisions")
        ).fetchone()
        groups = await (await conn.execute("select occurrence_count from alerts")).fetchall()
    assert processed == covered == 1000
    assert groups == [(1000,)]
    print(f"processed={processed} decision_rows={sum(counts.values())} by_outcome={counts}")
