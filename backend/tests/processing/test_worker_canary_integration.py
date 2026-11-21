from types import SimpleNamespace

import pytest
from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch
from psycopg.types.json import Jsonb


def event(source="ordinary", *, synthetic=False):
    return SimpleNamespace(source_id=source, trace={"synthetic": synthetic})


def rule(name, **extras):
    return {
        "id": name, "owner": "team:ops", "group": {"by": ["slot"]},
        "match": {"all": []}, "route": [{"to": "user:operator", "channels": ["sms"]}], **extras,
    }


CANARY = rule("delivery-canary", include_synthetic=True,
              route=[{"to": "user:canary", "channels": ["sms"]}])
DIRECTORY = {
    "teams": [{"id": "ops", "members": ["operator", "canary"]}],
    "users": [{"id": "operator", "contacts": {"sms": "+15550100555"}},
              {"id": "canary", "contacts": {"sms": "+15550100990"}}],
}


async def store(db, source, slot, state="firing", synthetic=False):
    async with db.connection() as conn:
        cursor = await conn.execute(
            """insert into events(source_id,transport,payload_hash,schema_id,raw,fields,trace,severity)
               values(%s,'http','hash','sample','{}',%s,%s,'warning') returning id""",
            (source, Jsonb({"slot": slot, "state": state, "trace.synthetic": True}),
             Jsonb({"synthetic": True} if synthetic else {})),
        )
        return (await cursor.fetchone())[0]


def test_standalone_view_fails_closed_and_cannot_promote_a_shadow(monkeypatch):
    from alerting.routing import event_policy

    monkeypatch.setattr(event_policy, "_allows_rule", None)
    live = (rule("live", include_synthetic=True), object())
    reserved = (CANARY, object())
    draft = (rule("draft", shadow=True), object())
    ordinary = SimpleNamespace(rules=[live])
    assert event_policy.eligible_ruleset(event(), ordinary) is ordinary
    legacy = SimpleNamespace(rules=[live, reserved, draft])  # Before native shadow_rules existed.
    assert event_policy.eligible_ruleset(event(), legacy).rules == [live]
    for source, marked in [("canary", False), ("canary", True), ("ordinary", True)]:
        assert event_policy.eligible_ruleset(event(source, synthetic=marked), legacy).rules == []
    assert legacy.rules == [live, reserved, draft]


def test_native_view_honors_marked_opt_in_for_live_and_shadow():
    pytest.importorskip("alerting.canary.policy")
    from alerting.routing.event_policy import eligible_ruleset

    normal = rule("ordinary")
    marked = rule("allowed", include_synthetic=True)
    draft = rule("draft", shadow=True, include_synthetic=True)
    ruleset = SimpleNamespace(rules=[(r, object()) for r in [normal, marked, CANARY]],
                              shadow_rules=[(r, object()) for r in [dict(normal, shadow=True), draft]])
    def ids(view):
        return [r["id"] for r, _ in view.rules], [r["id"] for r, _ in view.shadow_rules]
    assert ids(eligible_ruleset(event("ordinary", synthetic=True), ruleset)) == (["allowed"], ["draft"])
    assert ids(eligible_ruleset(event("canary", synthetic=True), ruleset)) == (["delivery-canary"], [])
    assert ids(eligible_ruleset(event("canary"), ruleset)) == ([], [])
    assert ids(eligible_ruleset(event("ordinary"), ruleset)) == (["ordinary", "allowed"], ["ordinary", "draft"])


async def test_worker_without_canary_policy_processes_only_real_notification(database, monkeypatch):
    from alerting.routing import event_policy

    monkeypatch.setattr(event_policy, "_allows_rule", None)
    cfg = Settings(Ruleset([CANARY, rule("operator-alert", include_synthetic=True),
                            rule("draft", shadow=True)], [{"id": "sample"}]), DIRECTORY, [])
    real = await store(database, "ordinary", "live")
    await store(database, "ordinary", "marked", synthetic=True)
    await store(database, "canary", "reserved")
    assert await process_batch(database, cfg) == 3
    async with database.connection() as conn:
        assert await (await conn.execute(
            """select a.representative_event_id,a.rule_id,n.recipient from alerts a
               join notifications n on n.alert_id=a.id"""
        )).fetchall() == [(real, "operator-alert", "operator")]
        assert await (await conn.execute(
            "select count(*) from events where status='processed'"
        )).fetchone() == (3,)


async def test_probe_recovery_cannot_resolve_an_ordinary_incident_in_a_mixed_claim(database):
    pytest.importorskip("alerting.canary.policy")
    cfg = Settings(Ruleset([rule("operator-alert"), CANARY], [
        {"id": "sample", "resolve_when": {"field": "state", "op": "eq", "value": "recovered"}},
    ]), DIRECTORY, [])
    await store(database, "ordinary", "shared")
    assert await process_batch(database, cfg) == 1
    probe = await store(database, "canary", "shared", "recovered")
    await store(database, "ordinary", "another")
    assert await process_batch(database, cfg) == 2
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select alert_key,status from alerts where rule_id='operator-alert' order by alert_key"
        )).fetchall() == [("another", "open"), ("shared", "open")]
        assert await (await conn.execute(
            "select count(*) from decisions where event_id=%s and rule_id='operator-alert'", (probe,)
        )).fetchone() == (0,)


async def test_mixed_claim_scopes_heartbeat_shadow_and_operator_outbox_to_ordinary_event(database, monkeypatch):
    from alerting.rules import worker

    pytest.importorskip("alerting.canary.policy")
    ruleset = Ruleset([
        CANARY, rule("operator-alert", include_synthetic=True),
        rule("liveness", expect={"key": ["slot"], "every": "5m"}, include_synthetic=True),
        rule("draft", shadow=True, include_synthetic=True),
    ], [{"id": "sample"}])
    if not hasattr(ruleset, "shadow_rules"):
        pytest.skip("requires native W1 shadow projection")
    locked_keys = set()
    lock_open = worker.lock_open

    async def record_keys(cur, keys):
        locked_keys.update(keys)
        return await lock_open(cur, keys)

    monkeypatch.setattr(worker, "lock_open", record_keys)
    real = await store(database, "ordinary", "real")
    probe = await store(database, "canary", "probe")
    assert await process_batch(database, Settings(ruleset, DIRECTORY, [])) == 2
    assert locked_keys == {("operator-alert", "real"), ("delivery-canary", "probe")}
    async with database.connection() as conn:
        assert await (await conn.execute("select alert_key,last_event_id from heartbeat_watches")).fetchall() == [
            ("real", real)]
        assert await (await conn.execute("select alert_key,occurrence_count from shadow_alerts")).fetchall() == [
            ("real", 1)]
        assert await (await conn.execute(
            "select rule_id from decisions where event_id=%s and rule_id is not null", (probe,)
        )).fetchall() == [("delivery-canary",)]
        assert await (await conn.execute(
            """select a.representative_event_id,n.recipient,n.state from alerts a
               join notifications n on n.alert_id=a.id order by a.representative_event_id"""
        )).fetchall() == [(real, "operator", "pending"), (probe, "canary", "pending")]
