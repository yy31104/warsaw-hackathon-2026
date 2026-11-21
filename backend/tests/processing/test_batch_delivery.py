import asyncio
from collections import Counter
from datetime import timedelta
from unittest.mock import Mock

import httpx

from alerting.clock import SYSTEM
from alerting.contracts import Clock
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import claim_batch, deliver_batch

from .test_fair_delivery import Gateway, queue_sms


async def test_two_workers_deliver_200_distinct_notifications_once(database):
    for team in ("settlements", "payments"):
        await queue_sms(database, team, 100, age=60)
    release = asyncio.Event()
    gateways = [Gateway(release, 1), Gateway(release, 1)]

    async def drain(gateway):
        while await deliver_batch(database, {"sms": gateway}, ["sms"], batch_size=25):
            pass

    async with asyncio.TaskGroup() as group:
        for gateway in gateways:
            group.create_task(drain(gateway))
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(gateway.ready.wait() for gateway in gateways))
            # Both workers have committed a complete lease before either provider replies.
            async with database.connection() as conn:
                leased = await (await conn.execute(
                    """select id from notifications where state='sending' and locked_by is not null
                       for update nowait"""
                )).fetchall()
            assert len(leased) == 50
        finally:
            release.set()

    async with database.connection() as conn:
        rows = await (await conn.execute(
            """select n.id::text,n.state,n.attempts,n.locked_by,n.locked_until,
                      a.attempt,a.outcome,a.finished_at is not null
               from notifications n left join delivery_attempts a on a.notification_id=n.id"""
        )).fetchall()
    assert len(rows) == 200
    assert all(row[1:] == ("delivered", 1, None, None, 1, "delivered", True) for row in rows)
    expected = {row[0] for row in rows}
    assert len(expected) == 200
    received = [key for gateway in gateways for _, key in gateway.receipts]
    assert Counter(received) == Counter(expected)


async def test_two_teams_alternate_for_two_full_batch_turns_each(database):
    await queue_sms(database, "flood", 100, age=60)
    await queue_sms(database, "quiet", 8, age=5)

    turns = []
    for _ in range(4):
        rows = await claim_batch(database, ["sms"], SYSTEM, batch_size=4)
        turns.append([row["team_id"] for row in rows])
    assert turns == [["flood"] * 4, ["quiet"] * 4] * 2


async def test_critical_arriving_during_normal_batch_is_next_to_send(database):
    await queue_sms(database, "normal", 12, age=60)
    release = asyncio.Event()
    gateway = Gateway(release, 1)
    providers = {"sms": gateway}
    slots = {"sms": asyncio.Semaphore(1)}
    first = asyncio.create_task(deliver_batch(
        database, providers, ["sms"], batch_size=4, semaphores=slots,
    ))
    try:
        await asyncio.wait_for(gateway.ready.wait(), 10)
        async with database.connection() as conn:
            rows = await (await conn.execute(
                "select state,count(*) from notifications group by state order by state"
            )).fetchall()
        assert rows == [("pending", 8), ("sending", 4)]
        await queue_sms(database, "critical", 1, age=0, priority=1)
    finally:
        release.set()
        await first
    assert [team for team, _ in gateway.receipts] == ["normal"] * 4
    assert await deliver_batch(database, providers, ["sms"], batch_size=4, semaphores=slots)
    assert gateway.receipts[4][0] == "critical"


async def test_expired_lease_mid_batch_reuses_receiver_key_and_fences_late_timeout(
    database, tmp_path, monkeypatch,
):
    monkeypatch.setenv("FAKE_TELECOM_DB", str(tmp_path / "receipts.sqlite3"))
    monkeypatch.setenv("FAKE_TELECOM_URL", "http://telecom.test")
    from mocks.fake_telecom.app import create_app

    await queue_sms(database, "+1-202-555-0102", 2, age=60)
    posts = []
    both_received = asyncio.Event()
    release = asyncio.Event()

    async def hold_first_reply(response):
        if response.request.url.path != "/sms":
            return
        response.raise_for_status()
        posts.append(response.request.headers["Idempotency-Key"])
        if len(posts) == 1:
            await release.wait()
            raise httpx.ReadTimeout("late timeout from the old owner", request=response.request)
        both_received.set()

    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://telecom.test",
        event_hooks={"response": [hold_first_reply]}, trust_env=False,
    ) as client:
        providers = {"sms": Telecom("sms", client)}
        first = asyncio.create_task(deliver_batch(
            database, providers, ["sms"], batch_size=2,
            semaphores={"sms": asyncio.Semaphore(2)},
        ))
        try:
            await asyncio.wait_for(both_received.wait(), 10)
            lost_key, sibling = posts
            # Only the first response is delayed. Move that lease across its deadline.
            async with database.connection() as conn:
                expired = await (await conn.execute(
                    """update notifications set locked_until=now()-interval '1 second'
                       where id=%s and state='sending' returning attempts""", (lost_key,),
                )).fetchone()
            assert expired == (1,)
            assert await deliver_batch(database, providers, ["sms"], batch_size=2)
            assert posts == [lost_key, sibling, lost_key]
        finally:
            release.set()
            await first
        receipts = (await client.get("/messages")).json()["items"]

    assert Counter(receipt["idempotency_key"] for receipt in receipts) == {lost_key: 1, sibling: 1}
    async with database.connection() as conn:
        notifications = await (await conn.execute(
            "select id::text,state,attempts,locked_by,locked_until from notifications"
        )).fetchall()
        attempts = await (await conn.execute(
            """select attempt,outcome,provider_ref,error,finished_at is not null
               from delivery_attempts where notification_id=%s order by attempt""", (lost_key,),
        )).fetchall()
        breakers = await (await conn.execute(
            "select failures from delivery_breakers where channel='sms'"
        )).fetchall()
    assert set(notifications) == {
        (lost_key, "delivered", 2, None, None), (sibling, "delivered", 1, None, None),
    }
    assert attempts == [
        (1, "unknown", None, "lease expired", True),
        (2, "delivered", lost_key, None, True),
    ]
    assert not breakers


async def test_expired_lease_waiting_for_channel_slot_is_sent_only_by_replacement(database):
    team = "settlements"
    await queue_sms(database, team, 2, age=60)
    clock = Mock(spec=Clock)
    clock.now.return_value = SYSTEM.now()
    release = asyncio.Event()
    first_gateway = Gateway(release, 1)
    first = asyncio.create_task(deliver_batch(
        database, {"sms": first_gateway}, ["sms"], clock, batch_size=2,
        semaphores={"sms": asyncio.Semaphore(1)},
    ))
    try:
        await asyncio.wait_for(first_gateway.ready.wait(), 10)
        async with database.connection() as conn:
            leased = await (await conn.execute(
                """select id::text from notifications where state='sending'
                   and locked_until > %s""", (clock.now(),),
            )).fetchall()
        assert len(leased) == 2
        (_, in_flight), = first_gateway.receipts
        waiting, = {row[0] for row in leased} - {in_flight}
        clock.now.return_value += timedelta(seconds=31)
    finally:
        release.set()
        await first
    assert first_gateway.receipts == [(team, in_flight)]

    replacement = Gateway()
    assert await deliver_batch(database, {"sms": replacement}, ["sms"], clock, batch_size=2)
    assert replacement.receipts == [(team, waiting)]
    async with database.connection() as conn:
        attempts = await (await conn.execute(
            """select a.notification_id::text,a.attempt,a.outcome,n.state
               from delivery_attempts a join notifications n on n.id=a.notification_id"""
        )).fetchall()
    assert set(attempts) == {
        (in_flight, 1, "delivered", "delivered"),
        (waiting, 1, "unknown", "delivered"),
        (waiting, 2, "delivered", "delivered"),
    }


async def test_ack_while_waiting_for_sms_slot_cancels_queued_escalation(database, api):
    await queue_sms(database, "settlements", 2, age=20)
    async with database.connection() as conn:
        rows = await (await conn.execute(
            "select id::text,alert_id from notifications order by next_attempt_at,id"
        )).fetchall()
        (first_id, alert_id), (escalation_id, _) = rows
        await conn.execute(
            """update notifications set kind='escalation',
               rendered=jsonb_set(rendered,'{meta,requires_open_alert}','true')
               where id=%s""", (escalation_id,),
        )
    release = asyncio.Event()
    gateway = Gateway(release, 1)
    task = asyncio.create_task(deliver_batch(
        database, {"sms": gateway}, ["sms"], batch_size=2,
        semaphores={"sms": asyncio.Semaphore(1)},
    ))
    try:
        await asyncio.wait_for(gateway.ready.wait(), 10)
        assert gateway.receipts == [("settlements", first_id)]
        async with database.connection() as conn:
            leased = await (await conn.execute(
                "select state,locked_by is not null from notifications where id=%s",
                (escalation_id,),
            )).fetchone()
        assert leased == ("sending", True)
        response = await asyncio.wait_for(
            api.post(f"/v1/alerts/{alert_id}/ack", headers={"X-User": "sam"}), 5,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "acknowledged"
    finally:
        release.set()
        await task

    assert gateway.receipts == [("settlements", first_id)]
    async with database.connection() as conn:
        final = await (await conn.execute(
            "select id::text,state,locked_by,locked_until from notifications"
        )).fetchall()
        cancelled_attempt = await (await conn.execute(
            """select outcome,provider_ref,finished_at is not null from delivery_attempts
               where notification_id=%s""", (escalation_id,),
        )).fetchall()
    assert set(final) == {
        (first_id, "delivered", None, None), (escalation_id, "cancelled", None, None),
    }
    assert cancelled_attempt == [(None, None, True)]


async def test_sms_outage_retries_one_probe_and_falls_back_after_30_seconds(database):
    await queue_sms(database, "settlements", 3, age=60, priority=1)
    async with database.connection() as conn:
        await conn.execute("update alerts set severity='critical'")
        await conn.execute("update notifications set recipient='sam'")
    clock = Mock(spec=Clock)
    start = SYSTEM.now()
    clock.now.return_value = start
    sms_calls = []
    phone_calls = []
    available = False

    def receive(request):
        key = request.headers["Idempotency-Key"]
        if request.url.path == "/call":
            phone_calls.append(key)
            return httpx.Response(200)
        sms_calls.append(key)
        if len(sms_calls) == 1:
            raise httpx.ReadTimeout("gateway did not acknowledge", request=request)
        return httpx.Response(200 if available else 503, headers={"Retry-After": "2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        providers = {channel: Telecom(channel, client) for channel in ("sms", "phone")}
        assert await deliver_batch(database, providers, ["sms"], clock, batch_size=8)
        assert len(set(sms_calls)) == 3
        async with database.connection() as conn:
            states = await (await conn.execute(
                "select id::text,state from notifications"
            )).fetchall()
            breaker = await (await conn.execute(
                "select failures,opened_at,probe_after from delivery_breakers where channel='sms'"
            )).fetchone()
        assert dict(states) == {sms_calls[0]: "unknown", sms_calls[1]: "retrying", sms_calls[2]: "retrying"}
        assert breaker == (3, start, start + timedelta(seconds=10))

        clock.now.return_value = start + timedelta(seconds=5)
        assert not await deliver_batch(database, providers, ["sms"], clock, batch_size=8)
        assert len(sms_calls) == 3
        clock.now.return_value = start + timedelta(seconds=11)
        assert await deliver_batch(database, providers, ["sms"], clock, batch_size=8)
        assert len(sms_calls) == 4 and sms_calls[3] in sms_calls[:3]
        assert not await deliver_batch(database, providers, ["sms"], clock, batch_size=8)

        clock.now.return_value = start + timedelta(seconds=31)
        available = True
        assert await deliver_batch(database, providers, ["sms"], clock, batch_size=8)
        assert await deliver_batch(database, providers, ["sms", "phone"], clock, batch_size=8)
        assert not await deliver_batch(database, providers, ["sms", "phone"], clock, batch_size=8)

    async with database.connection() as conn:
        states = await (await conn.execute(
            "select id::text,channel,state,fallback_of::text from notifications"
        )).fetchall()
        failures = await (await conn.execute(
            "select failures from delivery_breakers where channel='sms'"
        )).fetchall()
    assert not failures
    assert len(sms_calls) == 6 and set(sms_calls) == set(sms_calls[:3])
    assert Counter((channel, state) for _, channel, state, _ in states) == {
        ("sms", "delivered"): 2, ("sms", "fallback"): 1, ("phone", "delivered"): 1,
    }
    original, = [key for key, _, state, _ in states if state == "fallback"]
    replacement, = phone_calls
    assert (replacement, "phone", "delivered", original) in states


async def test_slow_sms_respects_channel_caps_and_other_channel_commits(database):
    await queue_sms(database, "settlements", 5, age=60)
    async with database.connection() as conn:
        await conn.execute(
            """update notifications set channel='phone'
               where id in (select id from notifications order by id limit 2)"""
        )
    release_sms = asyncio.Event()
    release_phone = asyncio.Event()
    sms = Gateway(release_sms, 1)
    phone = Gateway(release_phone, 2)
    task = asyncio.create_task(deliver_batch(
        database, {"sms": sms, "phone": phone}, ["sms", "phone"], batch_size=5,
        semaphores={"sms": asyncio.Semaphore(1), "phone": asyncio.Semaphore(2)},
    ))
    try:
        async with asyncio.timeout(10):
            await asyncio.gather(sms.ready.wait(), phone.ready.wait())
        assert len(sms.receipts) == 1
        assert len(phone.receipts) == 2
        async with database.connection() as conn:
            locks = await (await conn.execute(
                "select id from notifications where state='sending' for update nowait"
            )).fetchall()
            other_transactions = await (await conn.execute(
                """select pid,state from pg_stat_activity where pid<>pg_backend_pid()
                   and application_name=current_setting('application_name') and xact_start is not null"""
            )).fetchall()
        assert len(locks) == 5
        assert not other_transactions

        release_phone.set()
        async with asyncio.timeout(10):
            while True:
                async with database.connection() as conn:
                    states = await (await conn.execute(
                        "select channel,state,count(*) from notifications group by channel,state"
                    )).fetchall()
                if ("phone", "delivered", 2) in states:
                    break
                await asyncio.sleep(0.01)
        assert ("sms", "sending", 3) in states
        assert len(sms.receipts) == 1
    finally:
        release_phone.set()
        release_sms.set()
        await task
    assert len({key for _, key in sms.receipts}) == 3
    assert len({key for _, key in phone.receipts}) == 2


async def test_concurrent_batches_claim_only_one_half_open_probe(database):
    await queue_sms(database, "settlements", 12, age=20)
    async with database.connection() as conn:
        await conn.execute(
            """insert into delivery_breakers(channel,failures,opened_at,probe_after)
               values ('sms',5,now()-interval '20 seconds',now()-interval '1 second')"""
        )
    batches = await asyncio.gather(*(
        claim_batch(database, ["sms"], SYSTEM, batch_size=8) for _ in range(2)
    ))
    assert sum(map(len, batches)) == 1
    async with database.connection() as conn:
        rows = await (await conn.execute(
            "select state,attempts,count(*) from notifications group by state,attempts order by state"
        )).fetchall()
        attempts = await (await conn.execute("select count(*) from delivery_attempts")).fetchone()
    assert rows == [("pending", 0, 11), ("sending", 1, 1)]
    assert attempts == (1,)
