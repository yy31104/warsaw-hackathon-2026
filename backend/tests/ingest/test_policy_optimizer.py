from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from psycopg import sql
from psycopg.types.json import Jsonb

from alerting.simulate.optimizer import OBJECTIVES, candidates, pareto_options


FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/protected/optimizer_demo.yaml"


def test_candidate_space_is_bounded_and_only_scales_named_numeric_thresholds():
    current = {
        "id": "queue",
        "match": {"all": [{"$severity": "warning"}, {"field": "queue_age", "op": "gte", "value": 20}]},
        "group": {"cooldown": "2m"},
    }
    original = deepcopy(current)
    alternatives = list(candidates(current, current))
    assert len(alternatives) == 23
    assert current == original
    assert {draft["match"]["all"][1]["value"] for draft in alternatives} == {20, 25, 30}
    assert {draft["group"].get("mode", "immediate") for draft in alternatives} == {"immediate", "digest"}
    assert {draft["group"]["cooldown"] for draft in alternatives} == {"2m", "5m", "10m", "30m"}
    proposal = {**current, "description": "Review this threshold"}
    assert len(list(candidates(current, proposal))) == 24
    for match in ({"count": 20}, {"field": "$severity", "op": "gte", "value": "warning"}):
        baseline = {**current, "match": match}
        alternatives = list(candidates(baseline, baseline))
        assert len(alternatives) == 7
        assert all(draft["match"] == match for draft in alternatives)


def test_pareto_ranking_drops_dominated_and_equivalent_options_deterministically():
    def option(name, scores, diff):
        return {"draft": {"id": name}, "diff": [diff], "metrics": dict(zip(OBJECTIVES, scores, strict=True))}

    cheap = option("cheap", (2, 1, 0, 0, 1), "+a")
    equivalent = option("equivalent", (2, 1, 0, 0, 1), "+b")
    quiet = option("quiet", (4, 0, 0, 0, 1), "+c")
    dominated = option("dominated", (4, 2, 0, 0, 1), "+d")
    options = [quiet, dominated, equivalent, cheap]
    assert pareto_options(options) == pareto_options(reversed(options)) == [cheap, quiet]


@pytest.fixture
async def optimizer_demo(inbox_pool, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    fixture = yaml.safe_load(FIXTURE.read_text())
    rule, history = fixture["rule"], fixture["history"]
    first_at = datetime.fromisoformat(history["first_at"])
    events = []
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            insert into config_objects(kind,id,version,spec,content_hash,origin)
            values ('rule',%s,1,%s,'optimizer-demo','ui')
        """, (rule["id"], Jsonb(rule)))
        for i in range(history["count"]):
            at = first_at + timedelta(seconds=i * history["every_s"])
            cursor = await conn.execute("""
                insert into events(source_id,transport,payload_hash,raw,fields,severity,trace,status)
                values ('optimizer-fixture','http',%s,'{}',%s,'warning',%s,'processed') returning id
            """, (f"optimizer-{i}", Jsonb(history["fields"]), Jsonb({"demo_clock": at.isoformat()})))
            events.append((await cursor.fetchone())[0])
        cursor = await conn.execute("""
            insert into alerts(rule_id,rule_version,alert_key,severity,team_id,representative_event_id)
            values (%s,1,'acted-on','critical','rates',%s) returning id
        """, (rule["id"], events[1]))
        alert_id, = await cursor.fetchone()
        cursor = await conn.execute("""
            insert into action_runs(alert_id,action,approval,state,requested_by)
            values (%s,'open_ticket','none','succeeded','rita') returning id
        """, (alert_id,))
        run_id, = await cursor.fetchone()
        await conn.execute("""
            insert into audit_log(actor,action,target_kind,target_id,detail)
            values ('rita','action.execute','alert',%s,%s)
        """, (str(alert_id), Jsonb({"run_id": run_id, "ok": True})))
        cursor = await conn.execute("""
            insert into rule_proposals(rule_id,team_id,base_version,draft,rationale,source,proposed_by)
            values (%s,'rates',1,%s,'Tune the repeated warning','human','rita') returning id
        """, (rule["id"], Jsonb({**rule, "group": {**rule["group"], "cooldown": "6m"}})))
        proposal_id, = await cursor.fetchone()
    return rule, alert_id, proposal_id


async def test_optimizer_demo_uses_protection_and_keeps_acted_on_evidence_read_only(
    client, inbox_pool, optimizer_demo
):
    rule, alert_id, _ = optimizer_demo
    path = f"/v1/rules/{rule['id']}/optimize/preview"
    response = await client.post(path, json={"window": "24h"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert 0 < result["evaluated_count"] <= 24
    async with inbox_pool.connection() as conn:
        revision, = await (await conn.execute("SELECT revision FROM config_state")).fetchone()
    assert result["events_replayed"] == 18 and result["config_revision"] == revision
    assert result["baseline"] == {"planned_interruptions": 18, "night_interruptions": 18}
    best = result["options"][0]
    assert best["id"] == "B" and best["savings"]["planned_interruptions"] == 14
    assert best["savings"]["night_interruptions"] == 14
    assert best["protected_cases"] >= 4 and best["protected_violations"] == 0
    assert best["counterexample"] is None and best["metrics"]["added_delay_max_s"] == 0
    assert result["acted_on_incidents_reviewed"] == 1
    assert [row["alert_id"] for row in best["acted_on_incidents_affected"]] == [alert_id]
    rejected = result["best_rejected"]
    assert rejected["id"] == "A" and rejected["protected_violations"] > 0
    assert rejected["counterexample"]["recipient"] == "rita"
    assert rejected["counterexample"]["channel"] == "sms"
    assert rejected["counterexample"]["kind"] == "missing"

    edited = await client.post(path, json={"window": "24h", "draft": {"enabled": False}})
    assert edited.status_code == 200, edited.text
    blast_radius = edited.json()["draft_result"]
    assert blast_radius["pair_changes"]["counts"]["missing"] == 18
    assert blast_radius["counterexample"]["kind"] == "missing"
    async with inbox_pool.connection() as conn:
        for table, count in (("notifications", 0), ("delivery_attempts", 0), ("decisions", 0),
                             ("events", 18), ("audit_log", 1), ("rule_proposals", 1), ("config_objects", 1)):
            cursor = await conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            assert await cursor.fetchone() == (count,)


async def test_optimizer_proposal_visibility_and_version(client, inbox_pool, optimizer_demo):
    rule, _, proposal_id = optimizer_demo
    path = f"/v1/rules/{rule['id']}/optimize/preview"
    body = {"window": "24h", "proposal_id": proposal_id}
    assert (await client.post(path, json=body)).status_code == 401
    assert (await client.post(path, headers={"X-User": "sam"}, json=body)).status_code == 404
    permitted = await client.post(path, headers={"X-User": "rita"}, json=body)
    assert permitted.status_code == 200, permitted.text
    assert 0 < permitted.json()["evaluated_count"] <= 24

    async with inbox_pool.connection() as conn:
        await conn.execute("UPDATE rule_proposals SET base_version=2 WHERE id=%s", (proposal_id,))
    assert (await client.post(path, headers={"X-User": "rita"}, json=body)).status_code == 409
    assert (await client.post(path, json={"window": "0h"})).status_code == 422
