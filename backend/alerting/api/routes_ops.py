"""Database-backed dashboard and Prometheus endpoints."""

import asyncio
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from psycopg import sql
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.rows import dict_row

from alerting import llm
from alerting.api.routes_stream import stream_stats
from alerting.clock import demo_mode
from alerting.db import pool

router = APIRouter(tags=["ops"])
WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
StatsWindow = Annotated[str, Query(pattern=r"^[1-9][0-9]{0,3}[smhd]$")]
NOTIFICATION_STATES = ("pending", "sending", "delivered", "retrying", "unknown", "failed", "fallback", "cancelled")
LATENCY_BUCKETS = [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60]
# Keep the config registry and the tailer's committed input position across cases.
RESET_RUNTIME_TABLES = (
    "action_runs", "alert_occurrences", "alerts", "approval_failures", "audit_log",
    "decisions", "delivery_attempts", "delivery_breakers", "delivery_team_turns", "delivery_windows",
    "events", "heartbeat_watches", "incident_memory", "ingest_rejections",
    "notifications", "rule_proposals", "schema_issues", "shadow_alerts", "silences",
)
RESET_SQL = sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY RESTRICT").format(
    sql.SQL(", ").join(map(sql.Identifier, RESET_RUNTIME_TABLES))
)


@router.get("/v1/assist/status")
async def assist_status():
    return llm.status()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/v1/stats/funnel")
async def funnel(window: StatsWindow = "15m", db=Depends(pool)) -> dict:
    duration = timedelta(seconds=int(window[:-1]) * WINDOW_UNITS[window[-1]])
    snapshot = (await stream_stats(db, {None}))[None]
    async with db.connection() as conn:
        rows = await conn.execute("""
            SELECT count(*) AS accepted, count(*) FILTER (WHERE status='needs_review') AS needs_review,
                   count(*) FILTER (WHERE received_at >= now()-interval '2 seconds') AS recent,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM decisions d WHERE d.event_id=e.id AND d.reason @> '{"matched":true}'
                   )) AS matched
            FROM events e WHERE received_at >= now()-%s
        """, (duration,))
        accepted, needs_review, recent, matched = await rows.fetchone()
        rows = await conn.execute("""
            SELECT kind, count(*), count(*) FILTER (WHERE received_at >= now()-interval '2 seconds')
            FROM ingest_rejections WHERE received_at >= now()-%s GROUP BY kind
        """, (duration,))
        rejections = {}
        for kind, total, latest in await rows.fetchall():
            rejections[kind] = total
            recent += latest
        rows = await conn.execute("""
            SELECT count(*) FILTER (WHERE status='pending'),
                   count(*) FILTER (WHERE status='processed' AND
                       coalesce((trace->>'processed_at')::timestamptz,processed_at) >= now()-interval '2 seconds')
            FROM events
        """)
        processing_backlog, processed_recent = await rows.fetchone()
        rows = await conn.execute("""
            SELECT count(*) FILTER (WHERE status<>'resolved' AND severity='critical'),
                   count(*) FILTER (WHERE created_at BETWEEN now()-%s AND now())
            FROM alerts
        """, (duration,))
        critical, incidents_opened = await rows.fetchone()
        rows = await conn.execute("""
            SELECT state, count(*) FROM notifications
            WHERE coalesce(delivered_at,created_at) >= now()-%s GROUP BY state
        """, (duration,))
        notifications = dict.fromkeys(NOTIFICATION_STATES, 0)
        notifications.update(await rows.fetchall())
    return {
        **snapshot,
        "window": window,
        "raw": accepted + sum(rejections.values()),
        "accepted": accepted,
        "duplicates": rejections.get("DUPLICATE", 0),
        "conflicts": rejections.get("CONFLICT", 0),
        "rejected": rejections.get("REJECTED", 0),
        "needs_review": needs_review,
        "matched": matched,
        "critical": critical,
        "incidents_opened": incidents_opened,
        "notifications": notifications,
        "ingest_per_s": recent / 2,
        "processed_per_s": processed_recent / 2,
        "processing_backlog": processing_backlog,
    }


@router.get("/v1/stats/response")
async def response_stats(window: StatsWindow = "24h", team: str | None = None, db=Depends(pool)) -> dict:
    """Measure incident outcomes and delivery activity within the requested window."""
    duration = timedelta(seconds=int(window[:-1]) * WINDOW_UNITS[window[-1]])
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("""
            WITH bounds AS (
                SELECT now()-%(duration)s AS since, now() AS until, %(team)s::text AS team
            ), cohort AS (
                SELECT a.id, coalesce(a.first_seen_at, a.created_at) AS opened_at,
                       CASE WHEN a.acked_at <= b.until THEN a.acked_at END AS acked_at,
                       CASE WHEN a.resolved_at <= b.until THEN a.resolved_at END AS resolved_at,
                       a.resolve_reason,
                       CASE WHEN origin.outcome IS DISTINCT FROM 'HEARTBEAT_MISSED'
                            THEN coalesce(origin.received_at, e.received_at) END AS received_at
                FROM bounds b JOIN alerts a ON a.created_at BETWEEN b.since AND b.until
                LEFT JOIN events e ON e.id=a.representative_event_id
                LEFT JOIN LATERAL (
                    SELECT opened.received_at, d.outcome
                    FROM decisions d JOIN events opened ON opened.id=d.event_id
                    WHERE d.alert_id=a.id AND (
                        d.reason->>'transition'='created' OR d.outcome='HEARTBEAT_MISSED'
                    )
                    ORDER BY d.id LIMIT 1
                ) origin ON true
                WHERE (b.team IS NULL OR a.team_id=b.team)
                  AND e.trace->'synthetic' IS DISTINCT FROM 'true'::jsonb
            ), delivered AS (
                SELECT n.alert_id, coalesce(n.merged_into, n.id) AS send_id,
                       n.channel, n.recipient, n.delivered_at
                FROM bounds b JOIN notifications n ON n.delivered_at BETWEEN b.since AND b.until
                JOIN alerts a ON a.id=n.alert_id
                LEFT JOIN events e ON e.id=a.representative_event_id
                WHERE (b.team IS NULL OR a.team_id=b.team)
                  AND n.state='delivered' AND n.kind<>'canary'
                  AND e.trace->'synthetic' IS DISTINCT FROM 'true'::jsonb
            ), per_incident AS (
                SELECT c.id, c.acked_at, c.resolved_at, c.resolve_reason,
                       extract(epoch FROM (min(n.delivered_at) FILTER (
                           WHERE left(n.recipient, 5)<>'team:' AND n.delivered_at>=c.received_at
                       ) - c.received_at))::float8 AS tta,
                       CASE WHEN c.acked_at>=c.opened_at
                            THEN extract(epoch FROM c.acked_at-c.opened_at)::float8 END AS ack_s,
                       CASE WHEN c.resolved_at>=c.opened_at
                            THEN extract(epoch FROM c.resolved_at-c.opened_at)::float8 END AS resolve_s,
                       count(n.alert_id) AS notifications
                FROM cohort c LEFT JOIN delivered n ON n.alert_id=c.id
                GROUP BY c.id, c.acked_at, c.resolved_at, c.resolve_reason, c.opened_at, c.received_at
            ), interruptions AS (
                SELECT recipient, count(DISTINCT send_id) AS deliveries
                FROM delivered WHERE channel IN ('sms','phone') AND left(recipient, 5)<>'team:'
                GROUP BY recipient
            ), unrouted AS (
                SELECT count(*) AS total
                FROM bounds b JOIN decisions d ON d.created_at BETWEEN b.since AND b.until
                JOIN events e ON e.id=d.event_id
                LEFT JOIN alerts a ON a.id=d.alert_id
                WHERE d.outcome='UNROUTED' AND (b.team IS NULL OR a.team_id=b.team)
                  AND e.trace->'synthetic' IS DISTINCT FROM 'true'::jsonb
            )
            SELECT count(*) AS n_alerts, count(tta) AS n_tta, count(ack_s) AS n_mtta,
                   count(resolve_s) AS n_mttr, count(resolved_at) AS n_resolved,
                   percentile_cont(0.50) WITHIN GROUP (ORDER BY tta) AS tta_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY tta) AS tta_p95_s,
                   avg(ack_s) AS mtta_s, avg(resolve_s) AS mttr_s,
                   count(acked_at)::float8 / nullif(count(*), 0) AS ack_rate,
                   (SELECT total FROM unrouted) AS unrouted,
                   avg(notifications)::float8 AS notifications_per_alert,
                   count(*) FILTER (WHERE resolved_at IS NOT NULL AND resolve_reason='no_action_needed')::float8
                       / nullif(count(resolved_at), 0) AS no_action_rate,
                   (SELECT coalesce(jsonb_agg(
                       jsonb_build_object('user', recipient, 'count', deliveries)
                       ORDER BY deliveries DESC, recipient
                   ), '[]'::jsonb) FROM interruptions) AS interruptions
            FROM per_incident
        """, {"duration": duration, "team": team or None})
        result = await cur.fetchone()
    n_tta = result.pop("n_tta")
    n_alerts = result["n_alerts"]
    result["sample_counts"] = {
        "n_alerts": n_alerts,
        "tta_p50_s": n_tta,
        "tta_p95_s": n_tta,
        "mtta_s": result.pop("n_mtta"),
        "mttr_s": result.pop("n_mttr"),
        "ack_rate": n_alerts,
        "unrouted": result["unrouted"],
        "notifications_per_alert": n_alerts,
        "no_action_rate": result.pop("n_resolved"),
        "interruptions": sum(row["count"] for row in result["interruptions"]),
    }
    return {**result, "window": window}


class PrometheusText:
    def __init__(self):
        self.lines = []

    def family(self, name, kind, description):
        self.lines.extend((f"# HELP {name} {description}", f"# TYPE {name} {kind}"))

    def sample(self, name, value, **labels):
        # Labels here come from enums or the fixed bucket boundaries.
        suffix = "{" + ",".join(f'{key}="{label}"' for key, label in labels.items()) + "}" if labels else ""
        self.lines.append(f"{name}{suffix} {value}")

    def text(self):
        return "\n".join(self.lines) + "\n"


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(db=Depends(pool)) -> PlainTextResponse:
    delivery = (await stream_stats(db, {None}))[None]
    report = PrometheusText()
    async with db.connection() as conn:
        rows = await conn.execute("""
            SELECT lane, count(*), count(*) FILTER (WHERE status='processed'),
                   count(*) FILTER (WHERE status='pending') FROM events GROUP BY lane
        """)
        lanes = {lane: (accepted, processed, backlog) for lane, accepted, processed, backlog in await rows.fetchall()}
        report.family("events_ingested_total", "counter", "Events durably stored in the inbox.")
        report.sample("events_ingested_total", sum(values[0] for values in lanes.values()))
        report.family("events_processed_total", "counter", "Events marked processed by the rule workers.")
        report.sample("events_processed_total", sum(values[1] for values in lanes.values()))
        report.family("events_backlog", "gauge", "Events awaiting a rule worker.")
        for lane in ("normal", "critical"):
            report.sample("events_backlog", lanes.get(lane, (0, 0, 0))[2], lane=lane)
        rows = await conn.execute("SELECT kind, count(*) FROM ingest_rejections GROUP BY kind")
        report.family("events_ingest_rejections_total", "counter", "Duplicate, conflicting, or invalid arrivals.")
        for kind, count in await rows.fetchall():
            report.sample("events_ingest_rejections_total", count, kind=kind)

        rows = await conn.execute("SELECT state, count(*) FROM notifications GROUP BY state")
        counts = dict(await rows.fetchall())
        report.family("notifications_by_state", "gauge", "Current outbox rows by delivery state.")
        for state in NOTIFICATION_STATES:
            report.sample("notifications_by_state", counts.get(state, 0), state=state)
        rows = await conn.execute("""
            SELECT n.channel, coalesce(d.outcome,'in_progress'), count(*)
            FROM delivery_attempts d JOIN notifications n ON n.id=d.notification_id
            GROUP BY n.channel, d.outcome
        """)
        report.family("delivery_attempts_total", "counter", "Recorded attempts by channel.")
        attempts = await rows.fetchall()
        for channel in ("sms", "phone", "email", "ui"):
            report.sample("delivery_attempts_total", sum(count for ch, _, count in attempts if ch == channel), channel=channel)
        report.family("delivery_attempts_by_outcome", "gauge", "Recorded attempts by current outcome.")
        for channel, outcome, count in attempts:
            report.sample("delivery_attempts_by_outcome", count, channel=channel, outcome=outcome)
        await confirmation_histogram(conn, report)

    for metric, field, description in (
        ("delivery_backlog", "backlog", "Unfinished outbox rows."),
        ("oldest_pending_age_seconds", "oldest_pending_s", "Age of the oldest unfinished outbox row."),
        ("delivery_retries_5m", "retries_5m", "Retry attempts started in the last five minutes."),
        ("delivery_target_seconds", "delivery_target_s", "Delivery backlog age target."),
        ("delivery_at_risk", "at_risk", "Whether the backlog age exceeds the delivery target."),
    ):
        report.family(metric, "gauge", description)
        report.sample(metric, int(delivery[field]) if field == "at_risk" else delivery[field])
        for row in delivery["delivery"]:
            value = int(row[field]) if field == "at_risk" else row[field]
            report.sample(metric, value, channel=row["channel"], lane=row["lane"])
    return PlainTextResponse(report.text(), media_type="text/plain; version=0.0.4; charset=utf-8")


async def confirmation_histogram(conn, report):
    """Measure until the sender records confirmation. The benchmark uses receiver timestamps."""
    counters = sql.SQL(", ").join(
        sql.SQL("count(*) FILTER (WHERE latency <= %s)") for _ in LATENCY_BUCKETS
    )
    cursor = await conn.execute(sql.SQL("""
        WITH measured AS (
            SELECT CASE WHEN jsonb_typeof(rendered->'meta'->'gen_sent_at')='number'
                   THEN (extract(epoch FROM delivered_at) -
                        (rendered->'meta'->>'gen_sent_at')::numeric / 1000)::float8 END AS latency
            FROM notifications WHERE channel<>'ui' AND delivered_at IS NOT NULL
        )
        SELECT count(*), coalesce(sum(latency),0), {}
        FROM measured WHERE latency >= 0
    """).format(counters), LATENCY_BUCKETS)
    count, total, *buckets = await cursor.fetchone()
    name = "notification_confirmation_latency_seconds"
    report.family(name, "histogram", "Time from generator timestamp to recorded provider confirmation.")
    for boundary, cumulative in zip(LATENCY_BUCKETS, buckets, strict=True):
        report.sample(f"{name}_bucket", cumulative, le=boundary)
    report.sample(f"{name}_bucket", count, le="+Inf")
    report.sample(f"{name}_count", count)
    report.sample(f"{name}_sum", total)


@router.post("/v1/debug/reset")
async def reset(db=Depends(pool)) -> dict:
    if not demo_mode():
        raise HTTPException(404, "Demo reset is disabled")
    # A worker may still be committing the previous case. Release all reset
    # locks on contention before acquiring them again in a fresh transaction.
    for attempt in range(5):
        try:
            async with db.connection() as conn:
                await conn.execute("SET LOCAL lock_timeout = '500ms'")
                await conn.execute(RESET_SQL)
            return {"status": "reset"}
        except (LockNotAvailable, DeadlockDetected) as exc:
            if attempt == 4:
                raise HTTPException(409, "Demo reset is busy. Retry after the stack drains.") from exc
            await asyncio.sleep(0.1)
