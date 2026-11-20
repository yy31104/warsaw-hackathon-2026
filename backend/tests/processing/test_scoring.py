"""Synthetic same-window counters for a schema with arbitrary field names."""

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from alerting.clock import FixedClock
from alerting.contracts import Transition
from alerting.routing import plan
from alerting.rules.scoring import score


SCHEMA = {
    "slo": {"requests_total": "window.attempts", "errors_total": "window.failures", "target": 0.99}
}
RULE = {"priority": "P3", "impact": 6}
BASE_PARTS = {"severity": 35, "environment": 25, "impact": 6, "frequency": 10}


@pytest.mark.parametrize(
    ("requests", "errors", "points"),
    [(1000, 0, 0), (1000, 5, 0.5), ("1000", "10", 1), (1000, 30, 3), (1000, 1000, 25)],
)
def test_burn_is_error_fraction_over_allowed_fraction(requests, errors, points):
    fields = {"window.attempts": requests, "window.failures": errors}
    assert score("error", "prod", 100, RULE, fields=fields, schema=SCHEMA) == (
        "P3", 76 + points, {**BASE_PARTS, "error_budget_burn": points}
    )


def test_simulated_source_labels_its_component_and_uses_the_declared_objective():
    schema = {"slo": {**SCHEMA["slo"], "target": 0.95, "simulated": True}}
    fields = {"window": {"attempts": 1000, "failures": 100}}
    assert score("error", "prod", 100, RULE, fields=fields, schema=schema) == (
        "P3", 78, {**BASE_PARTS, "error_budget_burn_simulated": 2}
    )


@pytest.mark.parametrize(
    ("requests", "errors"),
    [
        (None, 1), (0, 0), (-10, 1), ("unknown", 1), (True, 0), ("Infinity", 1),
        (10.5, 1), (10, None), (10, -1), (10, 11), (10, 1.2), (10, False), (10, "NaN"),
    ],
)
def test_unusable_counts_preserve_the_complete_legacy_score(requests, errors):
    fields = {"window.attempts": requests, "window.failures": errors}
    assert score("error", "prod", 100, RULE, fields=fields, schema=SCHEMA) == (
        "P3", 76, BASE_PARTS
    )


@pytest.mark.parametrize(
    "slo",
    [
        None, {}, "unconfigured", {"target": 0.99}, {**SCHEMA["slo"], "target": None},
        {**SCHEMA["slo"], "errors_total": []}, {**SCHEMA["slo"], "target": 1},
        {**SCHEMA["slo"], "target": 0}, {**SCHEMA["slo"], "target": "NaN"},
    ],
)
def test_burn_requires_a_usable_schema_mapping_and_objective(slo):
    fields = {"window.attempts": 100, "window.failures": 5}
    assert score("error", "prod", 100, RULE, fields=fields, schema={"slo": slo}) == (
        "P3", 76, BASE_PARTS
    )


def test_absent_counter_fields_never_use_alert_occurrences_as_a_denominator():
    expected = ("P1", 81, {"severity": 35, "environment": 25, "impact": 6, "frequency": 15})
    assert score("error", "prod", 10000, {"impact": 6}) == expected
    assert score(
        "error", "prod", 10000, {"impact": 6}, fields={"window.failures": 20}, schema=SCHEMA
    ) == expected


def test_budget_burn_changes_feed_score_and_preserves_the_delivery_plan():
    clock = FixedClock(datetime(2026, 1, 2, tzinfo=UTC))
    rule = {**RULE, "id": "synthetic", "route": [{"to": "team:synthetic", "channels": ["ui"]}]}
    directory = {"teams": [{"id": "synthetic"}], "users": []}
    alert = dict(
        id=1, rule_id="synthetic", rule_version=1, alert_key="A", status="open", severity="error"
    )
    fields = {"window.attempts": 10, "window.failures": 10}
    plans, scores = [], []
    for schema in (None, SCHEMA):
        priority, total, _ = score("error", "prod", 100, rule, fields=fields, schema=schema)
        snapshot = {**alert, "priority": priority, "score": total}
        plans.append(plan(snapshot, Transition.created, rule, fields, directory, [], {}, clock))
        scores.append(total)
    assert scores == [76, 101]
    assert plans[0] == plans[1]
    assert [(msg.recipient, msg.channel, msg.priority) for msg in plans[1].intents] == [
        ("team:synthetic", "ui", 4)
    ]


async def test_budget_points_persist_in_jsonb_and_sort_the_postgres_alert_feed(database, api):
    schema = {"slo": {**SCHEMA["slo"], "simulated": True}}
    async with database.connection() as conn:
        for key, fields in [
            ("no-denominator", {"window.failures": 20}),
            ("burning", {"window.attempts": 1000, "window.failures": 20}),
        ]:
            priority, total, parts = score("error", "prod", 100, RULE, fields=fields, schema=schema)
            await conn.execute(
                """insert into alerts(rule_id,rule_version,alert_key,severity,priority,score,score_breakdown)
                   values ('synthetic',1,%s,'error',%s,%s,%s)""",
                (key, priority, total, Jsonb(parts)),
            )
    response = await api.get("/v1/alerts")
    response.raise_for_status()
    burning, unscored = response.json()["items"]
    assert [(row["alert_key"], row["priority"], row["score"]) for row in (burning, unscored)] == [
        ("burning", "P3", 78), ("no-denominator", "P3", 76)
    ]
    assert burning["score_breakdown"] == {**BASE_PARTS, "error_budget_burn_simulated": 2}
    assert unscored["score_breakdown"] == BASE_PARTS
