import json
from datetime import timedelta

import httpx
import pytest
from alerting.clock import SYSTEM, FixedClock
from alerting.config import files
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch

from .test_worker import seed


def test_nonmerging_or_throttled_intents_are_always_independent():
    from alerting.contracts import NotificationIntent, RenderedMessage
    from alerting.routing.merge import groups

    alert = {"rule_id": "a", "representative_event_id": 1}
    intent = NotificationIntent(alert_id=1, kind="initial", channel="sms", recipient="sam",
                                dedup_key="same", rendered=RenderedMessage(title="a", short="a", body="a"))
    for config, kind in [({}, "initial"), ({"merge_group": "desk"}, "throttle_summary"),
                         ({"merge_group": "desk"}, "initial")]:
        repeat = intent.model_copy(update={"kind": kind})
        assert [len(batch) for batch in groups([(alert, config, [repeat, repeat])])] == [1, 1]


def rule(name, group=None, user="sam", channel="sms"):
    result = {
        "id": name, "owner": "team:settlements",
        "match": {"field": "workflowId", "op": "exists"}, "group": {"by": ["workflowId"]},
        "present": {"title": name}, "route": [{"to": "user:" + user, "channels": [channel]}],
    }
    if group is not None:
        result["merge_group"] = group
    return result


async def test_critical_merge_sends_all_links_once_and_each_alert_keeps_history(database, api):
    names = [f"incident-{i}" for i in range(7)]
    cfg = Settings(Ruleset([rule(name, "desk") for name in names]), files.directory(), [])
    await seed(database, lane="critical")
    async with database.connection() as conn:
        await conn.execute("update events set severity='critical'")
    assert await process_batch(database, cfg, lane="critical") == 1
    async with database.connection() as conn:
        rows = await (await conn.execute(
            """select n.id,n.alert_id,a.rule_id,n.kind,n.merged_into,n.rendered,n.priority
               from notifications n join alerts a on a.id=n.alert_id order by a.rule_id"""
        )).fetchall()
        decisions = await (await conn.execute(
            "select alert_id,reason from decisions where outcome='MERGED'"
        )).fetchall()
        audit = await (await conn.execute(
            "select target_id,detail from audit_log where action='notification.merged'"
        )).fetchall()
    assert len(rows) == len(names)
    (primary,) = [row for row in rows if row[4] is None]
    assert {(r[2], r[3]) for r in rows} == {(name, "merged") for name in names}
    assert all(row[4] == primary[0] for row in rows if row != primary)
    assert primary[6] == 1
    assert {aid for aid, _ in decisions} == {r[1] for r in rows}
    assert {detail["notification_id"] for _, detail in decisions} == {str(r[0]) for r in rows}
    assert {target for target, _ in audit} == {str(r[1]) for r in rows}
    assert all(detail["physical_notification_id"] == str(primary[0]) for _, detail in audit)
    assert len(primary[5]["short"]) <= 160
    wire = []

    def telecom(request):
        wire.append((request.headers["Idempotency-Key"], json.loads(request.content)["text"]))
        if len(wire) == 1:
            raise httpx.ReadTimeout("after handoff", request=request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(telecom)) as client:
        providers = {"sms": Telecom("sms", client)}
        assert await deliver_one(database, providers, ["sms"])
        async with database.connection() as conn:
            assert await (await conn.execute("select distinct state,attempts from notifications")).fetchall() == [
                ("unknown", 1)]
        future = FixedClock(SYSTEM.now() + timedelta(seconds=20))
        assert await deliver_one(database, providers, ["sms"], future)
        assert not await deliver_one(database, providers, ["sms"], future)
    assert len(wire) == 2 and wire[0] == wire[1]
    assert wire[0][0] == str(primary[0])
    assert len(wire[0][1]) > 160
    for notification_id, aid, name, _, _, rendered, _ in rows:
        assert name in wire[0][1] and f"http://localhost:5173/a/{aid}" in wire[0][1]
        assert rendered["link"] == f"http://localhost:5173/a/{aid}"
        (item,) = (await api.get(f"/v1/alerts/{aid}/notifications")).json()["items"]
        assert (item["id"], item["state"], item["attempts"]) == (str(notification_id), "delivered", 2)
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select distinct notification_id from delivery_attempts"
        )).fetchall() == [(primary[0],)]


async def test_merge_has_strict_event_recipient_channel_and_group_boundaries(database):
    rules = [rule("a", "desk"), rule("b", "desk"), rule("no-opt-in"), rule("other-group", "other"),
             rule("other-user", "desk", user="lee"), rule("other-channel", "desk", channel="email")]
    await seed(database, count=2)
    cfg = Settings(Ruleset(rules), files.directory(), [])
    assert await process_batch(database, cfg) == 2
    async with database.connection() as conn:
        rows = await (await conn.execute(
            """select a.representative_event_id,a.rule_id,n.kind,n.id,n.merged_into
               from alerts a join notifications n on n.alert_id=a.id"""
        )).fetchall()
    assert len(rows) == 2 * len(rules)
    events = {r[0] for r in rows}
    assert len(events) == 2
    for event in events:
        part = [r for r in rows if r[0] == event]
        assert {(r[1], r[2]) for r in part} == {
            (r["id"], "merged" if r["id"] in ("a", "b") else "initial") for r in rules
        }
        (linked,) = [r for r in part if r[4] is not None]
        assert linked[4] in {r[3] for r in part if r[2] == "merged" and r[4] is None}


@pytest.mark.parametrize(("group", "expected_requests"), [(None, 7), ("desk", 1)])
async def test_same_event_and_seven_rules_measure_provider_handoffs(database, group, expected_requests):
    rules = [rule(f"incident-{i}", group) for i in range(7)]
    await seed(database)
    assert await process_batch(database, Settings(Ruleset(rules), files.directory(), [])) == 1
    keys = []

    def gateway(request):
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        providers = {"sms": Telecom("sms", client)}
        for _ in range(expected_requests):
            assert await deliver_one(database, providers, ["sms"])
        assert not await deliver_one(database, providers, ["sms"])
    assert len(set(keys)) == expected_requests
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select count(*) from notifications where state='delivered'"
        )).fetchone() == (7,)
