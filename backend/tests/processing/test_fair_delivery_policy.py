import asyncio
import json

import httpx
import pytest
from psycopg.types.json import Jsonb

from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one

from .test_fair_delivery import Gateway, queue_sms


@pytest.mark.parametrize("kind,closed,outage", [
    ("ladder_step", "acknowledged", False),
    ("escalation", "resolved", False),
    ("escalation", "acknowledged", True),
])
async def test_claim_and_critical_fallback_stop_after_ack(database, kind, closed, outage):
    await queue_sms(database, "test-team", 1, age=1)
    async with database.connection() as conn:
        await conn.execute("update alerts set severity='critical'")
        await conn.execute(
            "update notifications set kind=%s,rendered=jsonb_set(rendered,'{meta}',%s)",
            (kind, Jsonb({"requires_open_alert": True} if kind == "escalation" else {})),
        )
        if outage:
            await conn.execute(
                "insert into delivery_breakers values ('sms',5,now()-interval '35 seconds',now()+interval '1 minute')"
            )

    gateway = Gateway()
    async with database.connection() as ack:
        await ack.execute("update alerts set status=%s", (closed,))
        async with asyncio.timeout(2):
            await deliver_one(database, {"sms": gateway}, ["sms"])
    await deliver_one(database, {"sms": gateway}, ["sms"])
    assert not gateway.receipts
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select state,locked_by,attempts from notifications"
        )).fetchall() == [("cancelled", None, 0)]
        assert await (await conn.execute("select count(*) from delivery_attempts")).fetchone() == (0,)


async def test_merged_fallback_keeps_each_incident_link_and_uses_one_wire_request(database):
    for team in ["alpha", "beta"]:
        await queue_sms(database, team, 1, age=1, priority=1)
    async with database.connection() as conn:
        await conn.execute("update alerts set severity='critical'")
        await conn.execute("update notifications set kind='merged',channel='email',recipient='sam'")
        sources = await (await conn.execute(
            "select id,alert_id,contact from notifications order by alert_id"
        )).fetchall()
        members = [{"title": team, "link": f"http://localhost:5173/a/{aid}", "alert_id": aid}
                   for _, aid, team in sources]
        for index, (nid, _, team) in enumerate(sources):
            msg = {"title": team, "link": members[index]["link"], "body": "Email-only runbook detail",
                   "short": team, "meta": {} if index else {"merged_alerts": members}}
            await conn.execute("update notifications set rendered=%s,merged_into=%s where id=%s",
                               (Jsonb(msg), sources[0][0] if index else None, nid))
        await conn.execute(
            "insert into delivery_breakers values ('email',5,now()-interval '35 seconds',now()+interval '1 minute')"
        )

    assert await deliver_one(database, {}, ["email"])
    received = []

    def receive(request):
        received.append((request.headers["Idempotency-Key"], json.loads(request.content)["text"]))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        providers = {"phone": Telecom("phone", client)}
        assert await deliver_one(database, providers, ["phone"])
        assert not await deliver_one(database, providers, ["phone"])
    async with database.connection() as conn:
        original = await (await conn.execute(
            "select state from notifications where fallback_of is null order by alert_id"
        )).fetchall()
        replacements = await (await conn.execute(
            """select id,alert_id,merged_into,fallback_of,state,attempts,rendered->>'link'
               from notifications where fallback_of is not null order by alert_id"""
        )).fetchall()
    assert original == [("fallback",), ("fallback",)]
    assert len(replacements) == len(sources)
    primary = replacements[0][0]
    for i, (_, aid, linked, source, state, attempts, link) in enumerate(replacements):
        assert (source, aid) == sources[i][:2]
        assert (state, attempts, link) == ("delivered", 1, members[i]["link"])
        assert linked == (primary if i else None)
    text = "\n".join(f"{member['title']} {member['link']}" for member in members)
    assert received == [(str(primary), text + " — press 1 to acknowledge")]
