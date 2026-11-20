"""Team-filtered Server-Sent Events backed by one PostgreSQL listener per API instance."""
import asyncio
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from psycopg import sql

from alerting.clock import SYSTEM
from alerting.db import pool

log = logging.getLogger(__name__)
CHANNELS = {
    "alerts": {"alert.created", "alert.updated"},
    "notifications": {"notification.updated"},
    "actions": {"action.updated"},
    "schema_issues": {"schema.issue"},
    "config": {"config.changed"},
}
COUNT_INTERVAL = 0.4
TICK_INTERVAL = 2.0
HEARTBEAT = 15.0
CLIENT_BUFFER = 256
STATE_FIELDS = {"severity", "priority", "status"}
COUNTERS = {"occurrence_count", "count", "count_since_notify"}
SNAPSHOT_FIELDS = STATE_FIELDS | COUNTERS | {"id", "team_id", "ts", "score", "transition"}


def finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Expected a finite number")
    return number


def decode_notice(channel, payload):
    try:
        message = json.loads(payload, parse_float=finite_number, parse_constant=finite_number)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict):
        return None
    topic, data = message.get("type"), message.get("data")
    if channel == "config" and topic is None and {"kind", "id", "version"} <= message.keys():
        topic, data = "config.changed", {k: message[k] for k in ("kind", "id", "version", "team_id") if k in message}
    if not isinstance(topic, str) or topic not in CHANNELS.get(channel, ()) or not isinstance(data, dict):
        return None
    if topic == "schema.issue" and "id" not in data:
        data["id"] = data.get("fingerprint")
    if type(data.get("id")) not in (str, int) or not isinstance(data.get("team_id"), (str, type(None))):
        return None
    if not data.get("ts"):
        data["ts"] = SYSTEM.now().isoformat()
    return topic, data


@dataclass
class AlertWindow:
    state: dict
    timer: asyncio.TimerHandle
    pending: dict | None = None


class AlertCoalescer:
    def __init__(self, publish, call_later):
        self.publish = publish
        self.call_later = call_later
        self.windows = {}

    def push(self, topic, data):
        if topic not in ("alert.created", "alert.updated"):
            self.publish(topic, data)
            return
        key = data.get("team_id"), data["id"]
        window = self.windows.get(key)
        state = {name: data[name] for name in STATE_FIELDS if name in data}
        # The worker repeats severity, priority and status even for a plain count.
        count_only = (topic == "alert.updated" and bool(COUNTERS & data.keys())
                      and data.keys() <= SNAPSHOT_FIELDS and data.get("transition") in (None, "occurrence"))
        if window and count_only and all(window.state.get(k) == v for k, v in state.items()):
            window.pending = {**(window.pending or {}), **data}
            return
        if window:
            window.timer.cancel()
            if topic == "alert.updated":
                # Carry forward the latest counters; never emit an older open snapshot after a resolution.
                counters = {k: v for k, v in (window.pending or {}).items() if k in COUNTERS}
                data = {**counters, **data}
                state = {**window.state, **state}
        self.windows[key] = AlertWindow(state, self.call_later(COUNT_INTERVAL, self.flush, key))
        self.publish(topic, data)

    def flush(self, key):
        window = self.windows[key]
        if window.pending is None:
            del self.windows[key]
            return
        data, window.pending = window.pending, None
        window.timer = self.call_later(COUNT_INTERVAL, self.flush, key)
        self.publish("alert.updated", data)

    def clear(self):
        for window in self.windows.values():
            window.timer.cancel()
        self.windows.clear()


def event_frame(topic, data):
    return f"event: {topic}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


async def stream_stats(db, teams):
    """Sample once for all subscribers. Unrouted input volume is a global metric."""
    try:
        target = finite_number(os.getenv("DELIVERY_TARGET_S", "30"))
    except ValueError:
        target = 30.0
    if target < 0:
        target = 30.0
    ts = SYSTEM.now().isoformat()
    snapshots = {
        team: dict(id="funnel", team_id=team, ts=ts, alerts_open=0, backlog=0, retries_5m=0,
                   oldest_pending_s=0, delivery_target_s=target, at_risk=False, delivery=[])
        for team in teams
    }
    async with db.connection() as conn:
        rows = await conn.execute("""
            SELECT team_id, count(*) FROM alerts WHERE status <> 'resolved'
            GROUP BY GROUPING SETS ((team_id), ()) HAVING grouping(team_id)=1 OR team_id IS NOT NULL
        """)
        for team, count in await rows.fetchall():
            if team in snapshots:
                snapshots[team]["alerts_open"] = count
        if None in snapshots:
            row = await conn.execute("""
                SELECT count(*), count(*) FILTER (WHERE received_at >= now()-interval '2 seconds') / 2.0
                FROM (SELECT received_at FROM events WHERE received_at >= now()-interval '15 minutes'
                      UNION ALL SELECT received_at FROM ingest_rejections
                      WHERE received_at >= now()-interval '15 minutes') arrivals
            """)
            raw, rate = await row.fetchone()
            snapshots[None].update(window="15m", raw=raw, ingest_per_s=float(rate))
        rows = await conn.execute("""
            WITH delivery AS (
                SELECT a.team_id, n.channel, a.lane, count(*) backlog, min(n.created_at) oldest, 0 retries
                FROM notifications n JOIN alerts a ON a.id=n.alert_id
                WHERE n.state IN ('pending','retrying','unknown','sending')
                GROUP BY a.team_id,n.channel,a.lane
                UNION ALL
                SELECT a.team_id, n.channel, a.lane, 0, NULL, count(*)
                FROM delivery_attempts d JOIN notifications n ON n.id=d.notification_id
                JOIN alerts a ON a.id=n.alert_id
                WHERE d.attempt>1 AND d.started_at >= now()-interval '5 minutes'
                GROUP BY a.team_id,n.channel,a.lane
            )
            SELECT team_id,channel,lane,sum(backlog)::bigint,
                   greatest(0,extract(epoch FROM now()-min(oldest)))::float8,sum(retries)::bigint
            FROM delivery GROUP BY GROUPING SETS ((team_id,channel,lane),(channel,lane))
            HAVING grouping(team_id)=1 OR team_id IS NOT NULL ORDER BY channel,lane
        """)
        for team, channel, lane, backlog, oldest, retries in await rows.fetchall():
            if team not in snapshots:
                continue
            tick = snapshots[team]
            tick["delivery"].append(dict(channel=channel, lane=lane, backlog=backlog, oldest_pending_s=oldest,
                                         retries_5m=retries, delivery_target_s=target, at_risk=oldest > target))
            tick["backlog"] += backlog
            tick["retries_5m"] += retries
            tick["oldest_pending_s"] = max(tick["oldest_pending_s"], oldest)
            tick["at_risk"] = tick["at_risk"] or oldest > target
    return snapshots


class StreamHub:
    def __init__(self, db):
        self.db = db
        self.clients = {}
        self.interest = asyncio.Event()
        self.ready = asyncio.Event()
        self.tasks = []
        self.coalescer = AlertCoalescer(self.broadcast, asyncio.get_running_loop().call_later)

    def disconnect(self, queue):
        self.clients.pop(queue, None)
        if not self.clients:
            self.interest.clear()
            # Notices are ignored while idle; a later subscriber must start from a fresh snapshot.
            self.coalescer.clear()
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    def offer(self, queue, frame):
        if queue.full():
            # End this slow stream. EventSource reconnects and its onopen handler refetches current state.
            self.disconnect(queue)
        else:
            queue.put_nowait(frame)

    def broadcast(self, topic, data):
        frame = event_frame(topic, data)
        owner = data.get("team_id")
        global_config = topic == "config.changed" and owner is None
        for queue, team in tuple(self.clients.items()):
            if team is None or team == owner or global_config:
                self.offer(queue, frame)

    @asynccontextmanager
    async def subscribe(self, team):
        queue = asyncio.Queue(maxsize=CLIENT_BUFFER)
        self.clients[queue] = team or None
        self.interest.set()
        if not self.tasks:
            self.tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.ticks())]
        try:
            try:
                async with asyncio.timeout(5):
                    await self.ready.wait()
            except TimeoutError as exc:
                raise HTTPException(503, "Event stream is reconnecting", headers={"Retry-After": "1"}) from exc
            yield queue
        finally:
            self.disconnect(queue)

    async def listen(self):
        while True:
            await self.interest.wait()
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.db.conninfo, autocommit=True, connect_timeout=3
                ) as conn:
                    async with conn.transaction():
                        for channel in CHANNELS:
                            await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
                    self.ready.set()
                    batch_size = 0
                    async for notice in conn.notifies():
                        if self.clients and (event := decode_notice(notice.channel, notice.payload)):
                            self.coalescer.push(*event)
                        batch_size += 1
                        if batch_size == 64:
                            # A burst already buffered by psycopg must still let HTTP writers and timers run.
                            batch_size = 0
                            await asyncio.sleep(0)
            except (psycopg.Error, OSError):
                log.warning("PostgreSQL event stream disconnected", exc_info=True)
            if self.ready.is_set():
                self.ready.clear()
                self.coalescer.clear()
                # Invalidate every connection across a gap in LISTEN coverage.
                for queue in tuple(self.clients):
                    self.disconnect(queue)
            await asyncio.sleep(0.5)

    async def ticks(self):
        while True:
            await self.interest.wait()
            await asyncio.sleep(TICK_INTERVAL)
            if not self.clients or not self.ready.is_set():
                continue
            try:
                snapshots = await stream_stats(self.db, set(self.clients.values()))
                frames = {team: event_frame("stats.tick", data) for team, data in snapshots.items()}
                for queue, team in tuple(self.clients.items()):
                    if team in frames:
                        self.offer(queue, frames[team])
            except (psycopg.Error, OSError):
                log.warning("Could not sample stream statistics", exc_info=True)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.coalescer.clear()
        for queue in tuple(self.clients):
            self.disconnect(queue)


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        if hub := getattr(app.state, "alert_stream", None):
            await hub.close()
            del app.state.alert_stream


router = APIRouter(prefix="/v1", tags=["stream"], lifespan=lifespan)


async def subscribe(request: Request, team: str | None = None, db=Depends(pool)):
    hub = getattr(request.app.state, "alert_stream", None)
    if hub is None:
        hub = request.app.state.alert_stream = StreamHub(db)
    async with hub.subscribe(team) as queue:
        yield queue


async def stream_frames(queue):
    yield b"retry: 1000\n: connected\n\n"
    loop = asyncio.get_running_loop()
    heartbeat_at = loop.time() + HEARTBEAT
    while True:
        if loop.time() >= heartbeat_at:
            heartbeat_at = loop.time() + HEARTBEAT
            yield b": heartbeat\n\n"
        try:
            async with asyncio.timeout_at(heartbeat_at):
                frame = await queue.get()
        except TimeoutError:
            continue
        if frame is None:
            return
        yield frame


@router.get("/stream", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {}}}})
async def stream(queue=Depends(subscribe)):
    """Subscribe to team events, or all teams when omitted. Refetch the feed on every EventSource open."""
    return StreamingResponse(stream_frames(queue), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
