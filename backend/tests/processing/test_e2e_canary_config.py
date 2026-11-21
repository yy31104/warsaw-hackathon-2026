"""Check the deployed YAML through HTTP ingestion and the production rule worker."""

from uuid import uuid4

import pytest

from alerting import db
from alerting.api.app import app
from alerting.canary.monitor import CANARY_NUMBER
from alerting.config import files
from alerting.rules.worker import process_batch


@pytest.mark.e2e
async def test_bundled_canary_config_only_pages_reserved_number(api, database, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, db.pool, lambda: database)
    (source,) = (s for s in files.sources() if s["id"] == "canary")
    response = await api.post(
        "/v1/events/" + source["id"],
        headers={"X-Api-Key": source["api_key"]},
        json={"probe_id": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    assert await process_batch(database) == 1
    async with database.connection() as conn:
        assert await (
            await conn.execute("select schema_name,trace->'synthetic',status from events")
        ).fetchall() == [("system.canary", True, "processed")]
        assert await (
            await conn.execute(
                "select a.rule_id,n.recipient,n.channel,n.contact from notifications n "
                "join alerts a on a.id=n.alert_id"
            )
        ).fetchall() == [("delivery-canary", "canary", "sms", CANARY_NUMBER)]
