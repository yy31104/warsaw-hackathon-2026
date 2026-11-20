from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from alerting.clock import FixedClock
from alerting.contracts import CanonicalEvent
from alerting.rules.engine import Ruleset, evaluate
from alerting.rules.worker import Settings, process_batch


CLOCK = FixedClock(datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
RULE = {
    "id": "pressure",
    "match": {"phase": "high"},
    "group": {"by": ["rack"]},
    "route": [{"to": "team:plant", "channels": ["ui"]}],
}
SCHEMA = {"id": "meter", "episode_key": "{{cycle}}", "resolve_when": {"phase": "clear"}}
RULESET = Ruleset([RULE], [SCHEMA])


def event(**fields):
    return CanonicalEvent(
        source_id="meter-bus",
        transport="http",
        payload_hash="synthetic",
        schema_id=SCHEMA["id"],
        severity="critical",
        received_at=CLOCK.now(),
        raw={},
        fields={"rack": "west", "phase": "high", **fields},
    )


class History(dict):
    def __init__(self, open_alerts, resolved):
        super().__init__(open_alerts)
        self.resolved = resolved

    def get_resolved_episode(self, rule_id, key, episode):
        return self.resolved.get((rule_id, key, episode))


@pytest.mark.parametrize("with_new_open", [False, True])
def test_delayed_firing_is_linked_to_its_own_closed_alert(with_new_open):
    history = {("pressure", "west", "zulu"): 15, ("pressure", "west", "alpha"): 30}
    open_alert = {"id": 32, "status": "open", "severity": "warning", "latest_episode": "omega"}
    state = History({("pressure", "west"): open_alert} if with_new_open else {}, history)
    result = evaluate(event(cycle="zulu"), RULESET, state, CLOCK)

    assert result.alert_ops == []
    (decision,) = result.decisions
    assert (decision["outcome"], decision["alert_id"]) == ("LATE_OCCURRENCE", 15)
    assert decision["reason"]["episode"] == "zulu"
    assert decision["reason"]["transition"] == "late_occurrence"
    assert open_alert["latest_episode"] == "omega"


def test_resolved_snapshot_in_the_simulator_recognizes_the_canonical_episode():
    state = {("pressure", "west"): {"id": 15, "status": "resolved", "latest_episode": "past"}}
    result = evaluate(event(cycle="different", **{"$episode": "past"}), RULESET, state, CLOCK)
    assert result.alert_ops == []
    assert [(d["outcome"], d["alert_id"]) for d in result.decisions] == [("LATE_OCCURRENCE", 15)]


@pytest.mark.parametrize("fields", [{"cycle": "unseen"}, {"rack": "east", "cycle": "past"}, {}])
def test_other_group_new_episode_and_missing_episode_can_open_alerts(fields):
    state = History({}, {("pressure", "west", "past"): 15, ("temp", "west", "unseen"): 22})
    (op,) = evaluate(event(**fields), RULESET, state, CLOCK).alert_ops
    assert op.transition == "created" and op.notify


async def test_resolved_history_retains_all_firing_episodes_and_legacy_aliases(database):
    directory = {"teams": [{"id": "plant", "members": []}], "users": [], "fallback_team": "plant"}
    settings = Settings(RULESET, directory, [])
    observations = [
        event(cycle="first", **{"$episode": "legacy"}),
        event(cycle="middle"),
        event(cycle="last"),
        event(phase="clear", cycle="last"),
        event(cycle="current"),
    ]
    async with database.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """insert into events(source_id, transport, payload_hash, raw, fields, schema_id, severity)
               values (%s, 'http', 'synthetic', '{}', %s, %s, 'critical')""",
            [(e.source_id, Jsonb(e.fields), e.schema_id) for e in observations],
        )
    assert await process_batch(database, settings) == len(observations)
    async with database.connection() as conn:
        # Earlier worker versions recorded $episode on the event, before decisions carried it.
        await conn.execute("update decisions set reason = reason - 'episode' where event_id = 1")
        assert await (
            await conn.execute(
                "select rule_id, alert_key, episode, alert_id from resolved_alert_episodes order by episode"
            )
        ).fetchall() == [("pressure", "west", value, 1) for value in ("last", "legacy", "middle")]
