from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from alerting.rules.engine import Ruleset
from alerting.rules.worker import Settings, process_batch

pytest.importorskip("alerting.rules.shadow", reason="Requires the W1 shadow evaluator")

from .test_shadow import DRAFT, SCHEMA, START, rows, sample


async def test_worker_records_shadow_revisions_in_live_batch_without_dispatch(database):
    team = {"id": "synthetic", "members": []}
    route = [{"to": "team:synthetic", "channels": ["ui"]}]
    live = {**DRAFT, "id": "live", "shadow": False, "route": route}
    draft = {**DRAFT, "route": route}
    settings = Settings(Ruleset([live, draft, {**draft, "version": 4}], [SCHEMA]), {"teams": [team], "users": []}, [])
    for _ in range(3):
        await sample(database)
    assert await process_batch(database, settings, batch=2) == 2
    assert await process_batch(database, settings, batch=2) == 1
    assert await rows(database, "select rule_id,occurrence_count from alerts") == [("live", 3)]
    assert await rows(database, "select count(*) from notifications") == [(1,)]
    assert await rows(database, "select rule_version,occurrence_count from shadow_alerts order by 1") == [(3, 3), (4, 3)]
    assert await rows(database, """select rule_version,count(*),count(alert_id) from decisions
                                  where outcome='SHADOW_MATCH' group by rule_version order by 1""") == [(3, 3, 0), (4, 3, 0)]


async def test_shadow_only_worker_honors_draft_team_silence_and_fault_isolation(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    team = {"id": "synthetic", "members": []}
    draft = {**DRAFT, "owner": "team:synthetic"}
    invalid = {**draft, "id": "invalid", "match": {"field": "reading", "op": "invalid"}}
    settings = Settings(Ruleset([invalid, draft], [SCHEMA]), {"teams": [team], "users": []}, [])
    first = await sample(database)
    second = await sample(database)
    async with database.connection() as conn:
        await conn.execute(
            """insert into silences(team_id,matcher,include_critical,created_by,created_at,expires_at)
               values ('synthetic','{"unit":"A"}',false,'engineer',%s,%s)""", (START, START + timedelta(hours=1)),
        )
        for event, at in [(first, START), (second, START + timedelta(hours=1))]:
            await conn.execute("update events set trace=%s where id=%s", (Jsonb({"demo_clock": at.isoformat()}), event.id))
    assert await process_batch(database, settings) == 2
    assert await rows(database, """select reason->>'engine_outcome' from decisions
                                  where outcome='SHADOW_MATCH' order by event_id""") == [
        ("SILENCED",), ("EXISTING_ALERT",),
    ]
    assert await rows(database, """select count(*) from decisions where rule_id='invalid'
                                  and outcome='RULE_FAULT' and reason->>'shadow'='true'""") == [(2,)]
    assert await rows(database, "select distinct status from events") == [("processed",)]
    assert await rows(database, "select count(*) from alerts") == [(0,)]
    assert await rows(database, "select count(*) from notifications") == [(0,)]
