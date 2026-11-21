"""Inbox processing against W1's persisted and projected episode history."""

import pytest
from psycopg.types.json import Jsonb

pytest.importorskip("alerting.rules.silences", reason="requires the W1 rules integration")

from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch


async def batch(db, cfg, observations):
    async with db.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity)
               values('meter','http','synthetic','{}',%s,'meter','warning')""",
            [(Jsonb({"rack": "west", "phase": phase, "cycle": episode, "$episode": episode}),)
             for phase, episode in observations],
        )
    assert await process_batch(db, cfg) == len(observations)


@pytest.mark.parametrize("earlier_batch", [False, True])
async def test_late_firing_uses_closed_and_superseded_inbox_evidence(database, earlier_batch):
    cfg = Settings.load()
    rule = {"id": "gauge", "owner": "team:rates", "match": {"phase": "high"},
            "group": {"by": ["rack"]}, "route": [{"to": "user:rita", "channels": ["ui"]}]}
    schema = {"id": "meter", "episode_key": "{{cycle}}", "resolve_when": {"phase": "ok"}}
    cfg.ruleset = Ruleset([rule], [schema])
    firings = [("high", "first"), ("high", "last")]
    following = [("ok", "last"), ("high", "first"), ("high", "current"), ("high", "last")]
    if earlier_batch:
        await batch(database, cfg, firings)
        async with database.connection() as conn:
            # A legacy writer left episode identity only in the canonical event.
            await conn.execute("update decisions set reason=reason-'episode' where event_id=1")
        await batch(database, cfg, following)
    else:
        await batch(database, cfg, firings + following)
    await batch(database, cfg, [("high", "first"), ("high", "last")])
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select event_id,alert_id from decisions where outcome='LATE_OCCURRENCE' order by event_id"
        )).fetchall() == [(4, 1), (6, 1), (7, 1), (8, 1)]
        assert await (await conn.execute(
            "select status,occurrence_count,latest_episode from alerts order by id"
        )).fetchall() == [("resolved", 2, "last"), ("open", 1, "current")]
        assert await (await conn.execute("select count(*) from notifications")).fetchone() == (2,)


@pytest.mark.parametrize("close_before_batch", [False, True])
async def test_legacy_schema_episode_template_without_alias_or_decision_metadata(database, close_before_batch):
    cfg = Settings.load()
    rule = {"id": "vibration", "owner": "team:rates", "match": {"phase": "high"},
            "group": {"by": ["rack"]}, "route": [{"to": "user:rita", "channels": ["ui"]}]}
    schema = {"id": "meter", "episode_key": "{{cycle}}", "resolve_when": {"phase": "ok"}}
    cfg.ruleset = Ruleset([rule], [schema])
    await batch(database, cfg, [("high", "archived"), ("high", "newer")])
    async with database.connection() as conn:
        await conn.execute("update events set fields=fields-'$episode' where id=1")
        await conn.execute("update decisions set reason=reason-'episode' where event_id=1")
    if close_before_batch:
        await batch(database, cfg, [("ok", "newer")])
        await batch(database, cfg, [("high", "archived")])
    else:
        await batch(database, cfg, [("ok", "newer"), ("high", "archived")])
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select outcome,alert_id from decisions where event_id=4 and rule_id='vibration'"
        )).fetchone() == ("LATE_OCCURRENCE", 1)
        assert await (await conn.execute("select status,occurrence_count from alerts")).fetchall() == [
            ("resolved", 2)
        ]
