from datetime import timedelta

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alerting.clock import FixedClock
from alerting.contracts import Severity
from alerting.rules.engine import Ruleset, evaluate
from alerting.rules.failures import RuleFault
from alerting.rules.scoring import score

from .test_engine_silences import Maintenance, window
from .test_late_events import CLOCK, RULE, SCHEMA, event


POLICY = {"min_count": 10, "factor": 3, "window": "1m"}


def rules(**changes):
    return Ruleset([{**RULE, "group": {**RULE["group"], "cooldown": "1d"}, **changes}], [SCHEMA])


def send(state, ruleset, at, severity=Severity.warning):
    observation = event(**{"$severity": severity.value}).model_copy(update={"severity": severity})
    result = evaluate(observation, ruleset, state, FixedClock(at))
    (op,) = result.alert_ops
    (decision,) = result.decisions
    row = state.setdefault((op.rule["id"], op.key), {"id": 1, "status": "open"})
    row.update(severity=op.effective_severity, spike_state=op.spike_state)
    if op.notify:
        row["last_notified_at"] = at
    return op, decision


def replay(counts, ruleset, state, *, start=0, severity=Severity.warning):
    events = []
    for minute, count in enumerate(counts, start=start):
        for i in range(count):
            at = CLOCK.now() + timedelta(minutes=minute, microseconds=i)
            events.append(send(state, ruleset, at, severity))
    return events


def test_guarded_surge_pages_once_and_uses_the_higher_severity_for_priority():
    with_spike = rules(escalate_on_spike=POLICY)
    enabled = replay([2, 2, 200], with_spike, {})
    ordinary = replay([2, 2, 200], rules(), {})
    assert [op.transition for op, _ in ordinary if op.notify] == ["created"]
    assert all(op.spike_state is None for op, _ in ordinary)
    assert [op.transition for op, _ in enabled if op.notify] == ["created", "severity_increased"]
    (op, decision), = [(op, d) for op, d in enabled if op.transition == "severity_increased"]
    assert op.effective_severity == "error"
    assert score(op.effective_severity, None, 14, op.rule)[0] == "P1"
    assert decision["reason"]["spike"] == {
        "count": 10, "baseline": 2, "min_count": 10, "factor": 3, "window_seconds": 60,
    }


@pytest.mark.parametrize("counts", [[1, 1, 3], [20, 20, 40], [200]])
def test_low_absolute_traffic_subfactor_growth_or_cold_start_does_not_page(counts):
    events = replay(counts, rules(escalate_on_spike=POLICY), {})
    assert [op.transition for op, _ in events if op.notify] == ["created"]
    assert events[-1][0].effective_severity == "warning"


@pytest.mark.parametrize("level", [Severity.critical, Severity.unknown])
def test_spike_needs_a_comparable_higher_severity(level):
    events = replay([2, 10], rules(escalate_on_spike=POLICY), {}, severity=level)
    assert [op.transition for op, _ in events if op.notify] == ["created"]
    assert events[-1][0].effective_severity == level


def test_contiguous_spike_windows_page_once_and_rearm_after_a_quiet_window():
    events = replay([2, 2, 200, 300, 0, 500], rules(escalate_on_spike=POLICY), {})
    assert [op.effective_severity for op, _ in events if op.notify] == ["warning", "error", "critical"]
    # The 300/min window continues an existing burst. The second increase follows the empty window.
    assert [i for i, (op, _) in enumerate(events) if op.transition == "severity_increased"][1] > 503


def test_spike_to_critical_checks_silence_against_raised_severity():
    state = Maintenance(window(include_critical=False))
    events = replay([2, 10], rules(escalate_on_spike=POLICY), state, severity=Severity.error)
    assert events[0][1]["outcome"] == "SILENCED"
    op, decision = events[-1]
    assert op.effective_severity == "critical" and op.transition == "severity_increased" and op.notify
    assert decision["outcome"] == "EXISTING_ALERT"


def test_older_clock_and_changed_window_do_not_pollute_the_current_baseline():
    state = {}
    ruleset = rules(escalate_on_spike=POLICY)
    replay([2, 6, 1], ruleset, state)
    current = state["pressure", "west"]["spike_state"].copy()
    assert current["baseline"] == 3
    op, _ = send(state, ruleset, CLOCK.now())
    assert op.spike_state == current and not op.notify
    longer = rules(escalate_on_spike={**POLICY, "window": "5m"})
    op, _ = send(state, longer, CLOCK.now() + timedelta(minutes=2))
    assert op.spike_state["baseline"] is None and op.spike_state["count"] == 1


@pytest.mark.parametrize("bad", [{"min_count": 0}, {"factor": 1}, {"factor": float("nan")}, {"window": "0s"}])
def test_invalid_opt_in_is_attributed_to_its_rule(bad):
    with pytest.raises(RuleFault):
        send({}, rules(escalate_on_spike={**POLICY, **bad}), CLOCK.now())


async def test_persisted_window_resumes_after_postgres_round_trip(database):
    state = {}
    ruleset = rules(escalate_on_spike=POLICY)
    replay([2, 9], ruleset, state)
    row = state["pressure", "west"]
    async with database.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """insert into alerts(rule_id, rule_version, alert_key, severity, last_notified_at, spike_state)
               values ('pressure', 1, 'west', %s, %s, %s) returning *""",
            (row["severity"].value, row["last_notified_at"], Jsonb(row["spike_state"])),
        )
        persisted = await cur.fetchone()
    restored = {("pressure", "west"): persisted}
    op, _ = send(restored, ruleset, CLOCK.now() + timedelta(minutes=1, seconds=30))
    assert op.transition == "severity_increased" and op.notify
    assert op.spike_state["count"] == 10 and op.spike_state["baseline"] == 2
