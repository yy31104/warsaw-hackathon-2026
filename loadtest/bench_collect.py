"""Collect committed inbox/outbox rows and reconcile independent receiver receipts."""

import asyncio
import math
from collections import Counter
from urllib.parse import quote

import httpx
from psycopg.rows import dict_row

from loadtest.bench_generate import SOURCE, epoch_ms

EVENTS = "source_id=%s AND starts_with(source_event_id,%s)"
OUTBOX = """
    FROM notifications n JOIN alerts a ON a.id=n.alert_id
    JOIN events e ON e.id=a.representative_event_id
    WHERE e.source_id=%s AND starts_with(e.source_event_id,%s) AND n.channel IN ('sms','phone')
"""
OPEN_STATES = ("pending", "retrying", "unknown", "sending")
TERMINAL_STATES = ("delivered", "failed", "fallback", "cancelled")


class Collector:
    def __init__(self, conn, tag):
        self.conn = conn
        self.scope = (SOURCE, tag + "-")

    async def query(self, statement, params=None):
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(statement, params)
            return await cur.fetchall()

    async def progress(self):
        events, = await self.query(f"""
            SELECT count(*) AS accepted, count(*) FILTER (WHERE status='processed') AS processed,
                   count(*) FILTER (WHERE status='pending') AS processing_backlog
            FROM events WHERE {EVENTS}
        """, self.scope)
        outbox, = await self.query("""
            SELECT count(*) AS planned, count(*) FILTER (WHERE n.state='delivered') AS delivered,
                   count(*) FILTER (WHERE n.state=ANY(%s)) AS delivery_backlog,
                   count(*) FILTER (WHERE n.state=ANY(%s)) AS terminal,
                   count(*) FILTER (WHERE n.state IN ('failed','fallback','cancelled')) AS failed,
                   coalesce(sum(n.attempts),0)::bigint AS attempts,
                   greatest(0,extract(epoch FROM now()-min(n.created_at)
                       FILTER (WHERE n.state=ANY(%s))))::float8 AS oldest_pending_s
        """ + OUTBOX, (list(OPEN_STATES), list(TERMINAL_STATES), list(OPEN_STATES), *self.scope))
        return {"observed_at_ms": epoch_ms(), **events, **outbox}

    async def evidence(self):
        events = await self.query(f"""
            SELECT id, source_event_id, lane, status, (trace->>'gen_sent_at')::bigint AS gen_sent_at_ms,
                   (extract(epoch FROM received_at)*1000)::float8 AS received_at_ms,
                   (extract(epoch FROM coalesce((trace->>'processed_at')::timestamptz,processed_at))*1000)::float8
                        AS processed_at_ms
            FROM events WHERE {EVENTS} ORDER BY id
        """, self.scope)
        outbox = await self.query("""
            SELECT n.id::text, n.kind, n.channel, n.recipient, n.contact, n.state, n.attempts,
                   a.group_values->>'step' AS step, a.lane, e.source_event_id AS representative_event_id,
                   (n.rendered->'meta'->>'gen_sent_at')::bigint AS gen_sent_at_ms,
                   (extract(epoch FROM n.delivered_at)*1000)::float8 AS delivered_at_ms
        """ + OUTBOX + " ORDER BY step,n.channel,n.id", self.scope)
        attempts = await self.query("""
            SELECT d.notification_id::text, d.attempt, d.outcome, d.error,
                   (extract(epoch FROM d.started_at)*1000)::float8 AS started_at_ms,
                   (extract(epoch FROM d.finished_at)*1000)::float8 AS finished_at_ms
            FROM delivery_attempts d WHERE d.notification_id=ANY(%s::uuid[]) ORDER BY d.id
        """, ([notice["id"] for notice in outbox],))
        decisions, = await self.query(f"""
            SELECT count(*) AS count FROM decisions d JOIN events e ON e.id=d.event_id WHERE {EVENTS}
        """, self.scope)
        return events, outbox, attempts, decisions["count"]

    async def verified_fallbacks(self, outbox, mailpit):
        originals = [row["id"] for row in outbox if row["state"] == "fallback"]
        if not originals:
            return {"plans": [], "mailpit_receipts": [], "confirmed_original_ids": []}
        for _ in range(20):
            plans = await self.query("""
                SELECT id::text, fallback_of::text, channel, contact, state
                FROM notifications WHERE fallback_of=ANY(%s::uuid[]) ORDER BY id
            """, (originals,))
            if not any(row["state"] in OPEN_STATES for row in plans):
                break
            await asyncio.sleep(0.25)
        email_ids = {row["id"] for row in plans if row["channel"] == "email"}
        received = []
        if email_ids:
            async with httpx.AsyncClient(base_url=mailpit, timeout=5, trust_env=False) as mail:
                listing = await mail.get("/api/v1/messages", params={"limit": 100})
                listing.raise_for_status()
                for message in listing.json()["messages"]:
                    response = await mail.get(f"/api/v1/message/{quote(message['ID'], safe='')}/headers")
                    response.raise_for_status()
                    headers = {key.lower(): values for key, values in response.json().items()}
                    keys = headers.get("x-notification-id", [])
                    if len(keys) == 1 and keys[0] in email_ids:
                        received.append({
                            "mailpit_message_id": message["ID"], "notification_id": keys[0],
                            "recipients": [recipient["Address"] for recipient in message["To"]],
                        })
        email_receipts = {(row["notification_id"], address) for row in received for address in row["recipients"]}
        confirmed = {
            row["fallback_of"] for row in plans if row["state"] == "delivered" and (
                row["channel"] == "ui" or
                (row["channel"] == "email" and (row["id"], row["contact"]) in email_receipts)
            )
        }
        return {"plans": plans, "mailpit_receipts": received, "confirmed_original_ids": sorted(confirmed)}


def percentiles(values):
    values = sorted(values)
    return {
        "n": len(values),
        **{
            f"p{p}_ms": values[math.ceil(p * len(values) / 100) - 1] if values else None
            for p in (50, 95, 99)
        },
    }


def reconcile(expected_slots, outbox, receipts):
    plans = Counter((notice["step"], notice["channel"]) for notice in outbox)
    received = {receipt["idempotency_key"]: receipt for receipt in receipts}
    outbox_ids = {row["id"] for row in outbox}
    confirmed = set()
    invalid = []
    latencies = []
    for notice in outbox:
        receipt = received.get(notice["id"])
        if receipt is None:
            continue
        generated = notice["gen_sent_at_ms"]
        if (receipt["channel"] != notice["channel"] or receipt["to"] != notice["contact"]
                or generated is None or receipt["gen_sent_at"] != generated or receipt["received_at"] < generated):
            invalid.append(notice["id"])
            continue
        confirmed.add((notice["step"], notice["channel"]))
        latencies.append({
            "notification_id": notice["id"], "lane": notice["lane"],
            "latency_ms": receipt["received_at"] - generated,
        })
    lost = expected_slots - confirmed
    return {
        "lost_count": len(lost),
        "expected_slots": sorted(expected_slots),
        "lost_slots": sorted(lost),
        "missing_plan_slots": sorted(expected_slots - plans.keys()),
        "unexpected_plan_slots": sorted(plans.keys() - expected_slots),
        "duplicate_plan_slots": sorted(pair for pair, count in plans.items() if count > 1),
        "expected_outbox_ids": sorted(outbox_ids),
        "receiver_ids": sorted(received),
        "missing_receiver_ids": sorted(outbox_ids - received.keys()),
        "unexpected_receiver_ids": sorted(received.keys() - outbox_ids),
        "unconfirmed_outbox_ids": sorted(row["id"] for row in outbox if row["state"] != "delivered"),
        "invalid_receipts": sorted(invalid),
        "duplicate_receiver_count": len(receipts) - len(received),
        "e2e": percentiles(row["latency_ms"] for row in latencies),
        "critical_e2e": percentiles(row["latency_ms"] for row in latencies if row["lane"] == "critical"),
    }


def throughput(started_ms, duration_s, events, outbox, receipts):
    result = {}
    for name, rows, timestamp in (
        ("ingest", events, "received_at_ms"),
        ("processed", events, "processed_at_ms"),
        ("telecom_confirmed", outbox, "delivered_at_ms"),
        ("telecom_received", receipts, "received_at"),
    ):
        times = [row[timestamp] for row in rows if row[timestamp] is not None]
        # Include the entire offered window and any backlog drain after the last scheduled second.
        elapsed_s = max(duration_s, (max(times, default=started_ms) - started_ms) / 1000)
        result[name] = {
            "count": len(times), "elapsed_s": elapsed_s, "per_s": len(times) / elapsed_s,
            "within_offered_window": sum(started_ms <= stamp <= started_ms + duration_s * 1000 for stamp in times),
        }
    return result
