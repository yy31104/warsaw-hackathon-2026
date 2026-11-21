from unittest.mock import MagicMock

import pytest
from psycopg import OperationalError

from alerting.api import routes_rule_assist


ACTOR = {"X-User": "anna"}


@pytest.fixture
async def path_rule(client):
    # The two workflow names share a registry ID. Only one has the intended spellings.
    samples = [
        {"type": "SYNTHETIC_DENIED", "workflowId": "denied", "error": {"code": "LOW"},
         "notional": 50, "display_key": "desk"},
        {"type": "SYNTHETIC_ACCEPTED", "workflowId": "accepted", "errorCode": "REVIEWED",
         "xyz": True, "displayKey": "other"},
        {"meta": {"schema": "synthetic.trade"}, "payload": {"tradeId": "sample"}, "Notional": 40},
    ]
    for sample in samples:
        response = await client.post("/v1/events/generic-webhook", json=sample,
                                     headers={"X-Api-Key": "demo-generic-key"})
        assert response.status_code == 202 and response.json()["accepted"] == 1, response.text
    return {
        "id": "synthetic-path-check", "owner": "user:anna",
        "match": {"all": [
            {"field": "$schema", "op": "glob", "value": "SYNTHETIC_DEN*"},
            {"any": [{"errorCode": "LOW"}, {"not": {"$Severity": "info"}}]},
        ]},
        "group": {"by": ["Notional"]},
        # workflowName is a configured alias target, even though these samples omit it.
        "present": {"title": "{{displayKey}} {{errorCode}}", "fields": ["xyz", "workflowName", "$severity"]},
        "route": [{"to": "user:anna", "channels": ["ui"]}],
    }


def assert_suggestions(result):
    warnings = [warning for warning in result["warnings"] if warning.get("code") == "unknown_field"]
    assert warnings == result["problems"]
    paths = {warning["path"]: warning["suggestions"] for warning in warnings}
    assert len(warnings) == len(paths)
    assert set(paths) == {"errorCode", "$Severity", "Notional", "xyz", "displayKey"}
    assert paths["errorCode"][0] == "error.code"
    assert paths["$Severity"][0] == "$severity"
    assert paths["Notional"][0] == "notional"
    assert paths["displayKey"][0] == "display_key"
    assert paths["xyz"] == []
    assert all(warning["message"] and len(warning["suggestions"]) <= 3 for warning in warnings)


async def test_check_create_and_update_warn_without_editing_or_storing_the_advice(client, inbox_pool, path_rule):
    checked = await client.post("/v1/rules/check", json={"rule": path_rule})
    assert checked.status_code == 200, checked.text
    assert_suggestions(checked.json())
    assert any(w.get("problem") == "never fires" for w in checked.json()["warnings"])

    created = await client.post("/v1/rules", json=path_rule, headers=ACTOR)
    assert created.status_code == 201, created.text
    saved = created.json()
    assert_suggestions(saved)
    assert saved["match"] == path_rule["match"] and saved["present"] == path_rule["present"]
    assert_suggestions((await client.post("/v1/rules/check", json={"rule_id": saved["id"]})).json())

    updated = await client.put(f"/v1/rules/{saved['id']}", json=saved, headers=ACTOR)
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == saved["version"]
    assert_suggestions(updated.json())
    async with inbox_pool.connection() as conn:
        row = await conn.execute("select spec from config_objects where kind='rule' and id=%s and active",
                                 (saved["id"],))
        spec, = await row.fetchone()
    assert "warnings" not in spec and "problems" not in spec


async def test_approved_proposal_reports_the_same_advisory(client, inbox_pool, path_rule):
    base = {**path_rule, "match": {"$schema": "SYNTHETIC_DENIED"}}
    created = await client.post("/v1/rules", json=base, headers=ACTOR)
    assert created.status_code == 201, created.text
    async with inbox_pool.connection() as conn:
        row = await conn.execute("""
            insert into alerts (rule_id, rule_version, alert_key, team_id)
            values (%s, 1, 'preview', 'payments') returning id
        """, (base["id"],))
        alert_id, = await row.fetchone()
    proposal = await client.post("/v1/proposals", headers=ACTOR, json={
        "alert_id": alert_id, "text": "Review the updated fields", "draft": path_rule,
    })
    assert proposal.status_code == 201, proposal.text
    approved = await client.post(f"/v1/proposals/{proposal.json()['id']}/approve", headers=ACTOR)
    assert approved.status_code == 200, approved.text
    assert approved.json()["saved_rule"]["version"] == 2
    assert_suggestions(approved.json())
    assert_suggestions(approved.json()["saved_rule"])
    repeated = await client.post(f"/v1/proposals/{proposal.json()['id']}/approve", headers=ACTOR)
    assert repeated.status_code == 200 and repeated.json()["approved_version"] == 2
    assert_suggestions(repeated.json())


@pytest.mark.parametrize("match", [
    {"any": [
        {"$schema": "SYNTHETIC_DENIED", "errorCode": "LOW"},
        {"$schema": "SYNTHETIC_ACCEPTED", "xyz": True},
    ]},
    {"not": {"$schema": "SYNTHETIC_DENIED", "errorCode": "LOW"}},
])
async def test_boolean_groups_limit_each_field_to_its_own_schema_branch(client, path_rule, match):
    rule = {**path_rule, "group": {}, "present": {}, "match": match}
    checked = await client.post("/v1/rules/check", json={"rule": rule})
    assert checked.status_code == 200, checked.text
    problems = checked.json()["problems"]
    assert [problem["path"] for problem in problems] == ["errorCode"]
    assert problems[0]["suggestions"][0] == "error.code"


async def test_static_schema_aliases_are_known_before_the_first_event(client):
    rule = {
        "match": {"$schema": "alertmanager", "$transport": "http", "$source": "test"},
        "present": {"fields": ["labels.service", "$episode", "$severity", "payload.reason"]},
    }
    checked = await client.post("/v1/rules/check", json={"rule": rule})
    assert checked.status_code == 200, checked.text
    assert [problem["path"] for problem in checked.json()["problems"]] == ["payload.reason"]


async def test_recent_window_is_independent_for_each_rendered_schema(client, inbox_pool):
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            insert into events (source_id, transport, payload_hash, schema_id, schema_name, raw, fields, received_at)
            select 'test', 'http', 'synthetic', 'workflow', 'QUIET', '{}',
                   case when n=0 then '{"rotated": true}'::jsonb else '{"observed": true}'::jsonb end,
                   now() - interval '3 hours' + n * interval '1 minute'
            from generate_series(0, 50) n
        """)
        await conn.execute("""
            insert into events (source_id, transport, payload_hash, schema_id, schema_name, raw, fields, received_at)
            values ('test', 'http', 'aged', 'workflow', 'QUIET', '{}', '{"expired": true}', now()-interval '2 days')
        """)
        await conn.execute("""
            insert into events (source_id, transport, payload_hash, schema_id, schema_name, raw, fields)
            select 'test', 'http', 'synthetic', 'workflow', 'BUSY', '{}', '{"rotated": true, "expired": true}'
            from generate_series(1, 60)
        """)
    rule = {"match": {"$schema": "QUIET"}, "present": {"fields": ["observed", "rotated", "expired"]}}
    checked = await client.post("/v1/rules/check", json={"rule": rule})
    assert checked.status_code == 200, checked.text
    assert {problem["path"] for problem in checked.json()["problems"]} == {"rotated", "expired"}


async def test_unavailable_advisory_read_does_not_undo_a_valid_publication(client, path_rule, monkeypatch):
    unavailable = MagicMock()
    unavailable.__aenter__.side_effect = OperationalError("synthetic inventory timeout")
    monkeypatch.setattr(routes_rule_assist, "_read_only", lambda db: unavailable)
    created = await client.post("/v1/rules", json=path_rule, headers=ACTOR)
    assert created.status_code == 201 and created.json()["version"] == 1, created.text
    assert created.json()["warnings"] == []
    rules = (await client.get("/v1/rules")).json()["items"]
    assert next(rule for rule in rules if rule["id"] == path_rule["id"])["match"] == path_rule["match"]
