from datetime import timedelta

import pytest
from alerting.config import files
from alerting.routing import plan
from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch

from .test_golden import START
from .test_worker import seed


LADDER = ["ui", "email@5m", "sms@15m", "phone@30m"]
ALERT = dict(id=1, rule_id="ladder", rule_version=1, alert_key="key", status="open", severity="error")
PREFERENCES = {"to": "user:sam", "channels": "preferences"}
EXPLICIT = {"to": "user:lee", "channels": ["phone", "sms"]}


def destinations(rule, transition="created", alert=ALERT):
    result = plan(alert, transition, rule, {}, files.directory(), [], {}, START)
    return {(i.recipient, i.channel.value, i.kind) for i in result.intents}


def test_ladder_is_opt_in_and_offsets_are_absolute():
    from alerting.routing.ladder import steps

    rule = {"id": "ladder", "route": [PREFERENCES]}
    assert destinations(rule) == {("sam", "sms", "initial")}
    assert steps(rule) == ()
    rule["ladder"] = LADDER
    assert steps(rule) == (("ui", timedelta()), ("email", timedelta(minutes=5)),
                           ("sms", timedelta(minutes=15)), ("phone", timedelta(minutes=30)))
    assert destinations(rule) == {("sam", "ui", "ladder_step")}


def test_explicit_routes_take_precedence_at_each_step():
    from alerting.routing.ladder import step_rule, steps

    rule = {"id": "ladder", "ladder": LADDER, "route": [PREFERENCES, EXPLICIT]}
    assert destinations(rule) == {("sam", "ui", "ladder_step"), ("lee", "phone", "initial"),
                                  ("lee", "sms", "initial")}
    assert destinations(step_rule(rule, "email"), "ladder_step") == {("sam", "email", "ladder_step")}
    assert steps({**rule, "route": [EXPLICIT]}) == ()
    same_target = {**rule, "route": [PREFERENCES, {"to": "user:sam", "channels": ["phone"]}]}
    assert destinations(same_target) == {("sam", "phone", "initial")}
    assert destinations(step_rule(same_target, "email"), "ladder_step") == set()


def test_aliases_resolve_before_explicit_recipient_exclusion():
    from alerting.routing.ladder import step_rule, steps

    rule = {"id": "ladder", "ladder": LADDER, "route": [
        {"to": "team:settlements", "channels": "preferences"},
        {"to": "user:sam", "channels": ["phone"]},
    ]}
    assert destinations(rule) == {("sam", "phone", "initial"), ("lee", "ui", "ladder_step")}
    assert destinations(step_rule(rule, "sms"), "ladder_step") == {("lee", "sms", "ladder_step")}
    rule["route"][0]["to"] = "oncall:settlements"  # sam is also primary on-call.
    assert destinations(rule) == {("sam", "phone", "initial")}
    assert destinations(step_rule(rule, "email"), "ladder_step") == set()
    assert steps({**rule, "route": [{}]}) == ()


def test_first_digest_starts_the_ladder_and_preserves_occurrence_count():
    rule = {"id": "ladder", "ladder": LADDER, "group": {"mode": "digest", "every": "10m"},
            "route": [PREFERENCES, {"to": "user:lee", "channels": ["email"]}]}
    alert = {**ALERT, "count_since_notify": 7}
    assert destinations(rule, "created", alert) == set()
    first = plan(alert, "digest_due", rule, {}, files.directory(), [], {}, START)
    assert {(i.recipient, i.channel.value, i.kind) for i in first.intents} == {
        ("sam", "ui", "ladder_step"), ("lee", "email", "digest")}
    assert all("7 occurrences" in i.rendered.body for i in first.intents)
    assert destinations(rule, "digest_due", {**alert, "last_notified_at": START.now()}) == {
        ("sam", "ui", "digest"), ("lee", "email", "digest")}


@pytest.mark.parametrize("transition,kind", [("escalation", "escalation"),
                                            ("heartbeat_missed", "heartbeat")])
def test_digest_can_page_for_scheduler_transitions(transition, kind):
    rule = {"id": "ladder", "group": {"mode": "digest"}, "route": [EXPLICIT]}
    assert destinations(rule, transition) == {("lee", "phone", kind), ("lee", "sms", kind)}


async def test_first_ladder_step_persisted_alongside_explicit_route(database):
    rule = {"id": "ladder", "owner": "team:settlements", "ladder": LADDER,
            "match": {"field": "$severity", "op": "eq", "value": "error"}, "route": [PREFERENCES, EXPLICIT]}
    await seed(database)
    assert await process_batch(database, Settings(Ruleset([rule]), files.directory(), [])) == 1
    async with database.connection() as conn:
        rows = await (await conn.execute("select recipient,channel,state from notifications")).fetchall()
    assert set(rows) == {("sam", "ui", "delivered"), ("lee", "phone", "pending"), ("lee", "sms", "pending")}
