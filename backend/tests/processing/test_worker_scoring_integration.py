from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest
from psycopg.types.json import Jsonb

from alerting.rules.engine import AlertOp, Ruleset
from alerting.rules.scoring import score
from alerting.rules.worker import Settings, process_batch


START = datetime(2026, 1, 2, tzinfo=UTC)
DIRECTORY = {"teams": [{"id": "observed", "members": []}], "users": []}
RULE = {
    "id": "observed-failure", "owner": "team:observed", "match": {"reading": "high"},
    "group": {"by": ["unit"], "cooldown": "1d"},
    "route": [{"to": "team:observed", "channels": ["ui"]}],
}


async def insert_events(db, observations):
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """insert into events(source_id,transport,payload_hash,schema_id,severity,raw,fields,trace)
               values ('meter-source','http','synthetic',%s,%s,'{}',%s,%s)""",
            [(schema, severity, Jsonb({"reading": "high", **fields}),
              Jsonb({"demo_clock": at.isoformat()})) for schema, severity, fields, at in observations],
        )


@pytest.mark.skipif(not hasattr(AlertOp, "spike_state"), reason="Requires the W1 spike engine")
@pytest.mark.parametrize("baseline,last_count,expected", [(2, 200, "error"), (1, 3, "warning")])
async def test_worker_resumes_spike_window_across_committed_batches(database, monkeypatch, baseline, last_count, expected):
    monkeypatch.setenv("DEMO_MODE", "1")
    rule = {**RULE, "escalate_on_spike": {"min_count": 10, "factor": 3, "window": "1m"}}
    settings = Settings(Ruleset([rule], [{"id": "meter"}]), DIRECTORY, [])
    observations = [
        ("meter", "warning", {"unit": "west"}, START + timedelta(minutes=minute, microseconds=index))
        for minute, count in enumerate([baseline, baseline, last_count]) for index in range(count)
    ]
    split = min(13, len(observations) - 1)
    await insert_events(database, observations[:split])
    assert await process_batch(database, settings) == split
    await insert_events(database, observations[split:])
    assert await process_batch(database, settings) == len(observations) - split

    async with database.connection() as conn:
        severity, priority, count, state = await (await conn.execute(
            "select severity,priority,occurrence_count,spike_state from alerts"
        )).fetchone()
        notices = await (await conn.execute("select kind,priority from notifications order by kind")).fetchall()
        spikes = await (await conn.execute("select reason->'spike' from decisions where reason ? 'spike'")).fetchall()
    assert (severity, count, state.get("count"), state.get("baseline")) == (
        expected, len(observations), last_count, baseline,
    )
    if last_count == 200:
        assert priority == "P1" and notices == [("initial", 3), ("update", 2)]
        assert len(spikes) == 1 and spikes[0][0]["count"] == 10 and spikes[0][0]["baseline"] == 2
    else:
        assert priority == "P2" and notices == [("initial", 3)] and spikes == []


@pytest.mark.skipif("schema" not in signature(score).parameters, reason="Requires the W1 SLO scorer")
async def test_worker_scores_each_event_with_its_schema_and_observed_denominator(database):
    mapping = {"requests_total": "requests.window", "errors_total": "errors.window", "target": 0.99}
    schemas = [{"id": "real", "slo": mapping}, {"id": "sim", "slo": {**mapping, "simulated": True}}]
    settings = Settings(Ruleset([RULE], schemas), DIRECTORY, [])
    await insert_events(database, [
        ("real", "warning", {"unit": "real", "requests.window": 1000, "errors.window": 30}, START),
        ("sim", "warning", {"unit": "sim", "requests.window": 1000, "errors.window": 20}, START),
        ("real", "warning", {"unit": "missing", "errors.window": 100}, START),
    ])
    assert await process_batch(database, settings) == 3
    async with database.connection() as conn:
        rows = await (await conn.execute(
            "select alert_key,priority,score,score_breakdown from alerts order by alert_key"
        )).fetchall()
        notices = await (await conn.execute("select distinct channel,priority from notifications")).fetchall()
    missing, real, sim = rows
    assert [r[:2] for r in rows] == [("missing", "P2"), ("real", "P2"), ("sim", "P2")]
    assert set(missing[3]) == {"severity", "environment", "impact", "frequency"}
    assert real[3]["error_budget_burn"] == 3 and real[2] == missing[2] + 3
    assert sim[3]["error_budget_burn_simulated"] == 2 and sim[2] == missing[2] + 2
    assert notices == [("ui", 3)]
