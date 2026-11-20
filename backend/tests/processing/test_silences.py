from datetime import UTC, datetime, timedelta

import pytest

from alerting.api import routes_alerts
from alerting.clock import FixedClock
from alerting.rules.silences import active_silence, load_active


START = datetime(2026, 11, 20, 21, tzinfo=UTC)
MATCH = {"field": "service", "op": "eq", "value": "test-service"}


@pytest.fixture
def clock(monkeypatch):
    clock = FixedClock(START)
    monkeypatch.setattr(routes_alerts, "SYSTEM", clock)
    return clock


def payload(*, expires_at=START + timedelta(hours=1), **overrides):
    return {"team_id": "settlements", "match": MATCH, "expires_at": expires_at.isoformat(), **overrides}


async def test_silence_create_match_and_expire(api, database, clock):
    response = await api.post("/v1/silences", headers={"X-User": "sam"}, json=payload())
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["include_critical"] is False
    assert created["created_by"] == "sam"
    assert created["match"] == MATCH
    assert (await api.get("/v1/silences?team=settlements")).json() == {"items": [created]}
    assert (await api.get("/v1/silences?team=pricing")).json() == {"items": []}

    async with database.connection() as connection:
        silences = await load_active(connection, clock.now())
        assert len(silences) == 1
        silence = silences[0]
        assert silence.id == created["id"]
        assert silence.team_id == "settlements"
        assert active_silence(silences, {"service": "test-service"}, START, severity="warning") == silence
        assert active_silence(silences, {"service": "other-service"}, START, severity="warning") is None
        assert active_silence(silences, {"service": "test-service"}, START, severity="critical") is None
        assert active_silence(silences, {"service": "test-service"}, START - timedelta(seconds=1), severity="warning") is None
        assert active_silence(silences, {"service": "test-service"}, silence.expires_at, severity="warning") is None
        assert await load_active(connection, silence.expires_at) == ()


async def test_silence_delete_checks_membership_and_audits_once(api, database, clock):
    response = await api.post("/v1/silences", headers={"X-User": "sam"}, json=payload(include_critical=True))
    assert response.status_code == 201, response.text
    created = response.json()
    async with database.connection() as connection:
        silences = await load_active(connection, START)
    assert active_silence(silences, {"service": "test-service"}, START, severity="critical") is silences[0]
    url = f"/v1/silences/{created['id']}"
    assert (await api.delete(url, headers={"X-User": "pat"})).status_code == 403
    for _ in range(2):
        assert (await api.delete(url, headers={"X-User": "lee"})).status_code == 204
    assert (await api.get("/v1/silences")).json() == {"items": []}
    async with database.connection() as connection:
        assert await load_active(connection, START) == ()
        cursor = await connection.execute(
            "select actor, action from audit_log where target_kind='silence' and target_id=%s order by id",
            (str(created["id"]),),
        )
        assert await cursor.fetchall() == [("sam", "silence.create"), ("lee", "silence.delete")]


async def test_silence_rejects_invalid_match_expiry_and_team(api, clock):
    owner = {"X-User": "sam"}
    assert (await api.post("/v1/silences", headers=owner, json=payload(expires_at=START))).status_code == 422
    assert (await api.post(
        "/v1/silences", headers=owner, json=payload(match={"field": "service", "op": "unsupported"})
    )).status_code == 422
    assert (await api.post("/v1/silences", headers=owner, json=payload(team_id="pricing"))).status_code == 403
    assert (await api.get("/v1/silences")).json() == {"items": []}
