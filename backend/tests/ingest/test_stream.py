import asyncio
import json
import socket
from contextlib import AsyncExitStack
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg.conninfo import make_conninfo

from alerting.api import routes_stream
from alerting.api.routes_stream import AlertCoalescer, StreamHub, decode_notice, stream_frames, stream_stats
from alerting.db import pool


class Timers:
    """Advance the coalescing clock without sleeping."""

    def __init__(self):
        self.now = 0.0
        self.scheduled = {}

    def call_later(self, delay, fn, *args):
        callback = partial(fn, *args)
        self.scheduled[callback] = self.now + delay
        return SimpleNamespace(cancel=lambda: self.scheduled.pop(callback, None))

    def advance(self, until):
        while self.scheduled:
            callback = min(self.scheduled, key=self.scheduled.get)
            if self.scheduled[callback] > until:
                break
            self.now = self.scheduled.pop(callback)
            callback()
        self.now = until


def test_full_worker_snapshots_coalesce_per_alert_and_flush_the_latest_count():
    timers, sent = Timers(), []
    feed = AlertCoalescer(lambda topic, data: sent.append((timers.now, topic, data)), timers.call_later)
    base = dict(id=7, team_id="one", status="open", severity="critical", priority="P0")
    feed.push("alert.created", dict(base, occurrence_count=1))
    for count in range(2, 502):
        feed.push("alert.updated", dict(base, occurrence_count=count, score=count / 10))
    feed.push("alert.updated", dict(base, id=8, occurrence_count=12))
    timers.advance(0.399)
    assert [(topic, d["id"]) for _, topic, d in sent] == [("alert.created", 7), ("alert.updated", 8)]
    timers.advance(0.401)
    assert (sent[-1][0], sent[-1][2]["occurrence_count"]) == (0.4, 501)
    feed.push("alert.updated", dict(base, occurrence_count=502))
    timers.advance(0.801)
    assert [(at, d["occurrence_count"]) for at, _, d in sent if d["id"] == 7] == [
        (0, 1), (0.4, 501), (0.8, 502)
    ]


def test_urgent_changes_bypass_counts_and_a_stale_open_update_cannot_follow_resolution():
    timers, sent = Timers(), []
    feed = AlertCoalescer(lambda topic, data: sent.append((timers.now, topic, data)), timers.call_later)
    base = dict(id=7, team_id="one", status="open", severity="warning", priority="P2")
    feed.push("alert.created", dict(base, occurrence_count=1))
    feed.push("alert.updated", dict(base, occurrence_count=2))
    timers.advance(0.050)
    feed.push("alert.updated", dict(base, priority="P1", occurrence_count=3))
    feed.push("alert.updated", dict(base, severity="critical", priority="P0", occurrence_count=4))
    feed.push("alert.updated", dict(base, severity="critical", priority="P0", occurrence_count=5))
    feed.push("action.updated", dict(id=9, alert_id=7, team_id="one", state="pending_approval"))
    feed.push("alert.updated", dict(id=7, team_id="one", status="resolved"))
    timers.advance(1.0)
    assert [(at, topic) for at, topic, _ in sent] == [
        (0, "alert.created"), (.05, "alert.updated"), (.05, "alert.updated"),
        (.05, "action.updated"), (.05, "alert.updated"),
    ]
    assert sent[-1][2]["status"] == "resolved" and sent[-1][2]["occurrence_count"] == 5


def test_decode_uses_db_channel_and_accepts_the_shared_config_notice():
    topic, data = decode_notice("config", '{"kind":"schema","id":"vendor","version":2}')
    assert topic == "config.changed" and (data["id"], data["version"]) == ("vendor", 2)
    assert data["ts"]
    for payload in ("bad json", "[]", '{"type":"alert.created","data":{"id":[]}}',
                    '{"type":"action.updated","data":{"id":1}}',
                    '{"type":"alert.updated","data":{"id":1,"score":NaN}}',
                    '{"type":"alert.updated","data":{"id":1,"score":1e999}}'):
        assert decode_notice("alerts", payload) is None


async def through(queue, marker):
    rows = []
    async with asyncio.timeout(5):
        while True:
            frame = await queue.get()
            if frame is None:
                return rows
            topic, data = frame.decode().strip().split("\n", 1)
            row = (topic.removeprefix("event: "), json.loads(data.removeprefix("data: ")))
            rows.append(row)
            if row[1]["id"] == marker:
                return rows


async def test_two_team_subscribers_share_one_listener_and_only_see_committed_team_events(inbox_pool):
    appname = "stream-test-" + uuid4().hex
    db = SimpleNamespace(conninfo=make_conninfo(inbox_pool.conninfo, application_name=appname),
                         connection=inbox_pool.connection)
    hub = StreamHub(db)
    group, other, end = (uuid4().hex for _ in range(3))

    async def notify(conn, channel, topic, id_, team=None):
        data = {"id": id_, "team_id": team}
        await conn.execute("SELECT pg_notify(%s,%s)", (channel, json.dumps({"type": topic, "data": data})))

    async with AsyncExitStack() as stack:
        stack.push_async_callback(hub.close)
        first, second, third = [await stack.enter_async_context(hub.subscribe(t)) for t in (group, group, other)]
        async with inbox_pool.connection() as conn:
            pid, = await (await conn.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name=%s", (appname,))).fetchone()
            await notify(conn, "alerts", "alert.created", "discarded", group)
            await conn.rollback()
            for channel, topic, id_ in [("alerts", "alert.created", 11), ("notifications", "notification.updated", 12),
                                        ("actions", "action.updated", 13), ("schema_issues", "schema.issue", 14)]:
                await notify(conn, channel, topic, id_, group)
            await notify(conn, "alerts", "alert.created", 21, other)
            await notify(conn, "actions", "action.updated", "unscoped")
            await notify(conn, "config", "config.changed", end)
        for queue, ids in ((first, [11, 12, 13, 14, end]), (second, [11, 12, 13, 14, end]), (third, [21, end])):
            relevant = [data["id"] for topic, data in await through(queue, end)
                        if topic != "stats.tick" and (topic != "config.changed" or data["id"] == end)]
            assert relevant == ids
        async with inbox_pool.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name=%s",
                                             (appname,))).fetchone() == (1,)
            # Terminate only this test's dedicated LISTEN backend. Reconnecting clients must refetch.
            await conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        async with asyncio.timeout(5):
            for queue in (first, second, third):
                while await queue.get() is not None:
                    pass
        again = await stack.enter_async_context(hub.subscribe(group))
        async with inbox_pool.connection() as conn:
            await notify(conn, "alerts", "alert.created", end, group)
        assert (await through(again, end))[-1][0] == "alert.created"


async def test_slow_subscriber_reconnects_without_blocking_a_fast_one():
    hub = StreamHub(None)
    slow, fast = asyncio.Queue(2), asyncio.Queue(2)
    hub.clients = {slow: "team", fast: "team"}
    for item in range(6):
        hub.broadcast("notification.updated", {"id": item, "team_id": "team"})
        assert json.loads((await fast.get()).split(b"data: ")[1])["id"] == item
    assert await slow.get() is None and slow not in hub.clients


async def test_reconnecting_after_the_last_unsubscribe_cannot_receive_an_old_pending_count():
    hub, timers = StreamHub(None), Timers()
    hub.coalescer = AlertCoalescer(hub.broadcast, timers.call_later)
    old, new = asyncio.Queue(), asyncio.Queue()
    hub.clients[old] = "team"
    base = dict(id=1, team_id="team", status="open")
    hub.coalescer.push("alert.created", dict(base, occurrence_count=1))
    hub.coalescer.push("alert.updated", dict(base, occurrence_count=2))
    hub.disconnect(old)
    hub.clients[new] = "team"
    timers.advance(1)
    assert new.empty()


async def test_http_stream_flushes_urgent_frames_before_the_latest_counter(inbox_pool):
    app = FastAPI()
    app.include_router(routes_stream.router)
    app.dependency_overrides[pool] = lambda: inbox_pool
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", ws="none"))
    team = uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                async with client.stream("GET", "/v1/stream", params={"team": team}) as response:
                    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
                    assert response.headers["x-accel-buffering"] == "no"
                    lines = response.aiter_lines()
                    assert await anext(lines) == "retry: 1000"
                    events = [("alerts", "alert.created", 1, {"occurrence_count": 1})]
                    events += [("alerts", "alert.updated", 1, {"occurrence_count": i}) for i in range(2, 52)]
                    events += [
                        ("alerts", "alert.created", 2, {"severity": "warning", "occurrence_count": 1}),
                        ("alerts", "alert.updated", 2, {"severity": "critical", "occurrence_count": 2}),
                        ("actions", "action.updated", 3, {"alert_id": 2, "state": "pending_approval"}),
                        ("alerts", "alert.updated", 2, {"status": "resolved"}),
                    ]
                    async with inbox_pool.connection() as conn, conn.cursor() as cur:
                        await cur.executemany("SELECT pg_notify(%s, %s)", [
                            (channel, json.dumps({"type": topic, "data": dict(fields, id=id_, team_id=team)}))
                            for channel, topic, id_, fields in events
                        ])
                    received = []
                    async with asyncio.timeout(5):
                        async for line in lines:
                            if line.startswith("event: "):
                                topic = line[7:]
                            elif line.startswith("data: ") and topic != "stats.tick":
                                data = json.loads(line[6:])
                                received.append((topic, data["id"], data.get("occurrence_count")))
                                if data["id"] == 1 and data.get("occurrence_count") == 51:
                                    break
                    assert received == [
                        ("alert.created", 1, 1), ("alert.created", 2, 1), ("alert.updated", 2, 2),
                        ("action.updated", 3, None), ("alert.updated", 2, None), ("alert.updated", 1, 51),
                    ]
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=5)


async def test_heartbeat_is_sent_even_when_other_frames_arrive(monkeypatch):
    monkeypatch.setattr(routes_stream, "HEARTBEAT", 0.025)
    queue = asyncio.Queue()
    stream = stream_frames(queue)
    assert b": connected" in await anext(stream)
    await queue.put(b"first")
    assert await anext(stream) == b"first"
    assert await asyncio.wait_for(anext(stream), 1) == b": heartbeat\n\n"
    await queue.put(None)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_ticks_are_real_team_scoped_funnel_and_delivery_counts(inbox_pool, monkeypatch):
    monkeypatch.setenv("DELIVERY_TARGET_S", "30")
    async with inbox_pool.connection() as conn:
        alert, = await (await conn.execute("""
            INSERT INTO alerts(rule_id,rule_version,alert_key,severity,lane,team_id)
            VALUES ('test',1,'k','critical','critical','team-a') RETURNING id
        """)).fetchone()
        notice, = await (await conn.execute("""
            INSERT INTO notifications(alert_id,kind,channel,recipient,rendered,state,created_at)
            VALUES (%s,'initial','sms','demo','{}','retrying',now()-interval '60 seconds') RETURNING id
        """, (alert,))).fetchone()
        await conn.execute("INSERT INTO delivery_attempts(notification_id,attempt) VALUES(%s,2)", (notice,))
        await conn.execute("INSERT INTO events(source_id,transport,payload_hash,raw) VALUES('demo','http','a','{}')")
    views = await stream_stats(inbox_pool, {None, "team-a", "team-b"})
    for key in (None, "team-a"):
        tick = views[key]
        assert (tick["alerts_open"], tick["backlog"], tick["retries_5m"], tick["at_risk"]) == (1, 1, 1, True)
        delivery, = tick["delivery"]
        assert (delivery["channel"], delivery["lane"], delivery["backlog"]) == ("sms", "critical", 1)
        assert delivery["oldest_pending_s"] >= 59
    assert views[None]["raw"] == 1 and "raw" not in views["team-a"]
    assert (views["team-b"]["alerts_open"], views["team-b"]["delivery"], views["team-b"]["at_risk"]) == (0, [], False)
