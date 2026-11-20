import asyncio

from alerting.config import files
from psycopg.types.json import Jsonb

from .test_worker import Settings, process_batch, seed


async def test_concurrent_issuance_retries_collision(api, database, monkeypatch):
    from alerting.runbooks import approvals

    await seed(database)
    await process_batch(database, Settings.load())
    sequence = iter([1309, 1309, 4612])
    monkeypatch.setattr(approvals, "randbelow", lambda _: next(sequence))

    async def request():
        response = await api.post(
            "/v1/alerts/1/actions", json={"step_index": 2}, headers={"X-User": "sam"}
        )
        assert response.status_code == 200 and response.json()["state"] == "pending_approval"

    await asyncio.gather(request(), request())
    async with database.connection() as conn:
        assert await (
            await conn.execute("select approval_code from action_runs order by approval_code")
        ).fetchall() == [("001309",), ("004612",)]


async def test_legacy_duplicate_code_is_resolved_by_sender(api, database):
    directory = files.directory()
    users = {u["id"]: u for u in directory["users"]}
    teams = directory["teams"][:2]
    # Only this disposable schema simulates pre-constraint data. Production retains its unique index.
    async with database.connection() as conn:
        await conn.execute("drop index active_approval_code")
        expected = {}
        for team in teams:
            actor = team["approvers"][0]
            alert = await (
                await conn.execute(
                    """insert into alerts(rule_id,rule_version,alert_key,team_id)
                values('scoped-test',1,%s,%s) returning id""",
                    (team["id"], team["id"]),
                )
            ).fetchone()
            run = await (
                await conn.execute(
                    """insert into action_runs(alert_id,action,params,approval,state,
                approval_code,approval_expires_at,requested_by) values(%s,'escalate',%s,'required','pending_approval',
                '000315',now()+interval '5 minutes',%s) returning id""",
                    (alert[0], Jsonb({"team": team["id"]}), actor),
                )
            ).fetchone()
            expected[actor] = run[0]

    async def reply(actor):
        return await api.post(
            "/v1/inbound/sms", json={"from": users[actor]["contacts"]["sms"], "body": "YES 000315"}
        )

    replies = await asyncio.gather(*(reply(actor) for actor in expected))
    for (actor, id_), res in zip(expected.items(), replies, strict=True):
        assert res.status_code == 200, res.text
        assert (res.json()["id"], res.json()["approved_by"], res.json()["state"]) == (
            id_,
            actor,
            "succeeded",
        )
        assert (await reply(actor)).status_code in (404, 409)


async def test_two_replies_consume_code_once(api, database):
    await seed(database)
    await process_batch(database, Settings.load())
    await api.post("/v1/alerts/1/actions", json={"step_index": 2}, headers={"X-User": "sam"})
    async with database.connection() as conn:
        (pin,) = await (await conn.execute("select approval_code from action_runs")).fetchone()

    async def reply():
        return await api.post(
            "/v1/inbound/sms", json={"from": "+15550100003", "body": "YES " + pin}
        )

    replies = await asyncio.gather(reply(), reply())
    assert sorted(r.status_code for r in replies) in ([200, 404], [200, 409])
    async with database.connection() as conn:
        assert await (
            await conn.execute("select count(*) from audit_log where action='action.execute'")
        ).fetchone() == (1,)


async def test_callback_secret_and_persistent_lock(api, database, monkeypatch):
    import secrets
    from datetime import timedelta

    secret = secrets.token_urlsafe()
    monkeypatch.setenv("INBOUND_SECRET", secret)
    await seed(database)
    await process_batch(database, Settings.load())
    await api.post("/v1/alerts/1/actions", json={"step_index": 2}, headers={"X-User": "sam"})
    async with database.connection() as conn:
        pin, lifetime = await (
            await conn.execute(
                "select approval_code, approval_expires_at - created_at from action_runs"
            )
        ).fetchone()
    assert len(pin) == 6 and pin.isdecimal()
    assert abs(lifetime - timedelta(minutes=5)) < timedelta(seconds=2)
    api.headers.pop("X-Provider-Secret")
    sender = "+15550100003"
    wrong = "000001" if pin == "000000" else "000000"

    async def reply(code, headers):
        return await api.post(
            "/v1/inbound/sms", headers=headers, json={"from": sender, "body": "NO " + code}
        )

    for hdr in ({}, {"X-Provider-Secret": "incorrect"}):
        assert (await reply(pin, hdr)).status_code == 401
    headers = {"X-Provider-Secret": secret}
    results = await asyncio.gather(*(reply(wrong, headers) for _ in range(5)))
    assert sorted(r.status_code for r in results) == [404, 404, 404, 404, 429]
    assert (await reply(pin, headers)).status_code == 429
    async with database.connection() as conn:
        assert await (
            await conn.execute("select count(*) from audit_log where action='approval.locked'")
        ).fetchone() == (1,)
        assert await (await conn.execute("select state from action_runs")).fetchone() == (
            "pending_approval",
        )
        await conn.execute("update approval_failures set failed_at=failed_at-interval '16 minutes'")
    assert (await reply(pin, headers)).json()["state"] == "rejected"
