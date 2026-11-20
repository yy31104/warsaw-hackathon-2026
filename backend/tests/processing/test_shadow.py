"""Live dry-run evidence from explicitly synthetic events in the isolated PostgreSQL fixture."""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import shake_256

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import FixedClock
from alerting.contracts import CanonicalEvent, Outcome
from alerting.rules.engine import Ruleset
from alerting.rules.shadow import evaluate_shadow
from alerting.rules.silences import Silence
from alerting.rules.worker import Settings, process_batch


START = datetime(2026, 1, 2, tzinfo=UTC)
SCHEMA = {
    "id": "shadow-synthetic",
    "episode_key": "{{cycle}}",
    "resolve_when": {"reading": "recovered"},
}
DRAFT = {
    "id": "synthetic-capacity",
    "version": 3,
    "shadow": True,
    "match": {"reading": "firing"},
    "group": {"by": ["unit"], "cooldown": "1m"},
    "route": [{"to": "user:synthetic", "channels": ["sms"]}],
}


async def sample(database, *, unit="A", reading="firing", cycle="one", severity="error"):
    fields = {"unit": unit, "reading": reading, "cycle": cycle, "$severity": severity}
    async with database.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """insert into events(source_id,transport,payload_hash,schema_id,severity,raw,fields)
               values ('synthetic','http','synthetic',%s,%s,'{}',%s) returning *""",
            (SCHEMA["id"], severity, Jsonb(fields)),
        )
        return CanonicalEvent.model_validate(await cur.fetchone())


async def project(database, event, ruleset, seconds=0, *, silences_for_rule=None):
    clock = FixedClock(START + timedelta(seconds=seconds))
    async with database.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return await evaluate_shadow(cur, event, ruleset, clock, silences_for_rule=silences_for_rule)


async def rows(database, query):
    async with database.connection() as conn:
        return await (await conn.execute(query)).fetchall()


async def test_shadow_matches_own_counts_and_cooldowns_and_leaves_live_alert_intact(database):
    async with database.connection() as conn:
        await conn.execute(
            """insert into alerts(rule_id,rule_version,alert_key,severity,occurrence_count,last_notified_at)
               values (%s,3,'A','critical',50,%s)""",
            (DRAFT["id"], START),
        )
    live_before = await rows(database, "select * from alerts")
    ruleset = Ruleset([DRAFT], [SCHEMA])
    events = [await sample(database) for _ in range(3)]
    for event, seconds in zip(events, [0, 59, 60], strict=True):
        assert await project(database, event, ruleset, seconds) == 1

    decisions = await rows(database, "select outcome,alert_id,reason from decisions order by event_id")
    assert [(outcome, alert_id) for outcome, alert_id, _ in decisions] == [
        (Outcome.SHADOW_MATCH, None)
    ] * 3
    assert [
        (r["engine_outcome"], r["transition"], r["notify_eligible"], r["occurrence_count"])
        for _, _, r in decisions
    ] == [
        ("NEW_ALERT", "created", True, 1),
        ("SUPPRESSED_COOLDOWN", "occurrence", False, 2),
        ("EXISTING_ALERT", "cooldown_elapsed", True, 3),
    ]
    assert all(r["alert_key"] == "A" and r["trace"][0]["matched"] for _, _, r in decisions)
    assert await rows(database, "select occurrence_count,last_notified_at from shadow_alerts") == [
        (3, START + timedelta(minutes=1))
    ]
    assert await rows(database, "select * from alerts") == live_before
    assert await rows(database, "select id from notifications") == []
    assert await rows(database, "select event_id from alert_occurrences") == []


async def test_shadow_revisions_groups_and_nonmatches_are_independent(database):
    old = Ruleset([DRAFT], [SCHEMA])
    new = Ruleset([dict(DRAFT, version=4)], [SCHEMA])
    event = await sample(database)
    assert await project(database, event, old) == 1
    assert await project(database, event, old, 1) == 0
    assert await project(database, event, new, 1) == 1
    assert await project(database, await sample(database, unit="B"), old, 2) == 1
    mixed = Ruleset(
        [DRAFT, dict(DRAFT, id="live", shadow=False), dict(DRAFT, id="disabled", enabled=False)],
        [SCHEMA],
    )
    unmatched = await sample(database, reading="other", severity="critical")
    assert await project(database, unmatched, mixed) == 0
    assert await project(database, await sample(database, unit="B"), mixed, 3) == 1
    assert await rows(
        database, "select rule_id,rule_version,alert_key,occurrence_count from shadow_alerts order by 2,3"
    ) == [
        (DRAFT["id"], 3, "A", 1),
        (DRAFT["id"], 3, "B", 2),
        (DRAFT["id"], 4, "A", 1),
    ]
    assert await rows(database, "select count(*) from decisions") == [(4,)]
    assert await rows(database, "select count(*) from alerts") == [(0,)]


async def test_normal_worker_cannot_create_an_alert_or_page_for_a_shadow_rule(database):
    event = await sample(database, severity="critical")
    ruleset = Ruleset([DRAFT], [SCHEMA])
    directory = {"teams": [], "users": [{"id": "synthetic", "contacts": {"sms": "synthetic-sms"}}]}
    assert await project(database, event, ruleset) == 1
    assert await process_batch(database, Settings(ruleset, directory, [])) == 1
    assert await rows(database, "select status from events") == [("processed",)]
    assert await rows(database, "select count(*) from alerts") == [(0,)]
    assert await rows(database, "select count(*) from notifications") == [(0,)]
    assert await rows(database, "select outcome from decisions where rule_id is not null") == [
        (Outcome.SHADOW_MATCH,)
    ]


async def test_shadow_transactions_serialize_concurrent_occurrences_and_replays(database):
    ruleset = Ruleset([DRAFT], [SCHEMA])
    samples = [await sample(database) for _ in range(6)]
    counts = await asyncio.gather(*(project(database, e, ruleset) for e in samples + samples))
    assert sum(counts) == len(samples)
    assert await rows(database, "select occurrence_count from shadow_alerts") == [(6,)]
    assert await rows(
        database, "select (reason->>'notify_eligible')::boolean,count(*) from decisions group by 1 order by 1"
    ) == [(False, 5), (True, 1)]


async def test_shadow_and_decision_share_the_callers_transaction(database):
    ruleset = Ruleset([DRAFT], [SCHEMA])
    event = await sample(database)
    async with database.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        assert await evaluate_shadow(cur, event, ruleset, FixedClock(START)) == 1
        await conn.rollback()
    assert await rows(database, "select count(*) from shadow_alerts") == [(0,)]
    assert await rows(database, "select count(*) from decisions") == [(0,)]
    assert await project(database, event, ruleset) == 1


async def test_shadow_silence_and_digest_use_separate_pending_counts(database):
    silence = Silence.from_row(
        {
            "id": 7, "team_id": "synthetic", "match": {"unit": "A"},
            "include_critical": False, "created_by": "synthetic", "created_at": START,
            "expires_at": START + timedelta(hours=1),
        }
    )

    def silences_for_rule(rule):
        return (silence,) if rule["id"] == DRAFT["id"] else ()

    digest = dict(DRAFT, id="digest", group={**DRAFT["group"], "mode": "digest", "every": "1m"})
    ruleset = Ruleset([DRAFT, digest], [SCHEMA])
    assert await project(database, await sample(database), ruleset, silences_for_rule=silences_for_rule) == 2
    assert await rows(
        database, "select rule_id,occurrence_count,count_since_notify,last_notified_at from shadow_alerts order by 1"
    ) == [("digest", 1, 1, None), (DRAFT["id"], 1, 0, None)]
    assert await rows(database, "select reason->>'engine_outcome' from decisions order by rule_id") == [
        ("DIGEST_QUEUED",), ("SILENCED",)
    ]
    assert await project(
        database, await sample(database), ruleset, 3600, silences_for_rule=silences_for_rule
    ) == 2
    assert await rows(
        database,
        """select rule_id,count_since_notify,last_notified_at from shadow_alerts order by 1""",
    ) == [("digest", 2, None), (DRAFT["id"], 0, START + timedelta(hours=1))]
    assert await rows(
        database, "select reason->>'notification_cause' from decisions where reason ? 'notification_cause'"
    ) == [("first_eligible_after_silence",)]


async def test_shadow_resolution_and_late_evidence_use_only_draft_history(database):
    ruleset = Ruleset([DRAFT], [SCHEMA])
    first_episode = shake_256(b"synthetic episode").hexdigest(4000)
    sequence = [
        ("firing", first_episode),
        ("recovered", "other"),
        ("recovered", first_episode),
        ("firing", "two"),
        ("recovered", "two"),
        ("firing", "three"),
        ("firing", first_episode),
    ]
    for seconds, (reading, cycle) in enumerate(sequence):
        event = await sample(database, reading=reading, cycle=cycle)
        assert await project(database, event, ruleset, seconds) == 1
    assert await rows(database, "select reason->>'engine_outcome' from decisions order by event_id") == [
        ("NEW_ALERT",), ("STALE_RESOLVE",), ("RESOLVED",), ("NEW_ALERT",), ("RESOLVED",),
        ("NEW_ALERT",), ("LATE_OCCURRENCE",),
    ]
    assert await rows(
        database, "select status,latest_episode,occurrence_count from shadow_alerts order by id"
    ) == [("resolved", first_episode, 1), ("resolved", "two", 1), ("open", "three", 1)]


async def test_faulty_shadow_rule_keeps_valid_drafts_and_live_state_independent(database):
    invalid = dict(DRAFT, id="broken", match={"field": "reading", "op": "invalid"})
    ruleset = Ruleset([invalid, DRAFT], [SCHEMA])
    event = await sample(database)
    assert await project(database, event, ruleset) == 1
    assert await project(database, event, ruleset) == 0
    assert await rows(database, "select rule_id,outcome from decisions order by rule_id") == [
        ("broken", Outcome.RULE_FAULT), (DRAFT["id"], Outcome.SHADOW_MATCH)
    ]
    assert await rows(database, "select status from events") == [("pending",)]
    assert await rows(database, "select count(*) from notifications") == [(0,)]
