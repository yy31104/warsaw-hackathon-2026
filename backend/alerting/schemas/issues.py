"""Group accepted unknown events and create one durable UI notice per unfamiliar shape."""
import json
from collections import defaultdict

from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.contracts import CanonicalEvent
from alerting.schemas.mapping import fingerprint

REVIEW_RULE = "system:needs-review"


async def notify(conn, channel, event_type, **data):
    data["ts"] = SYSTEM.now().isoformat()
    message = {"type": event_type, "data": data}
    if channel == "config":
        # The config listener consumes the §4 fields; SSE consumes the §10 envelope.
        message.update({key: data[key] for key in ("kind", "id", "version")})
    await conn.execute("SELECT pg_notify(%s, %s)", (channel, json.dumps(message)))


async def _open_notice(conn, shape, sample, count, first_seen, last_seen):
    team = files.directory()["fallback_team"]
    title = "New event format needs review"
    body = f"Review the field mapping for a sample from {sample.source_id}."
    cursor = await conn.execute("""
        INSERT INTO alerts (rule_id, rule_version, alert_key, severity, priority, team_id, title, body, color, fields,
                            occurrence_count, representative_event_id, first_seen_at, last_seen_at)
        VALUES (%s,1,%s,'unknown','P3',%s,%s,%s,'grey',%s,%s,%s,%s,%s) RETURNING id
    """, (REVIEW_RULE, shape, team, title, body, Jsonb({"fingerprint": shape}),
          count, sample.id, first_seen, last_seen))
    alert_id, = await cursor.fetchone()
    rendered = {"title": title, "body": body, "short": title, "color": "grey", "fields": {"fingerprint": shape}}
    cursor = await conn.execute("""
        INSERT INTO notifications (alert_id, kind, recipient, channel, rendered, state, delivered_at, dedup_key)
        VALUES (%s,'initial',%s,'ui',%s,'delivered',now(),%s) RETURNING id
    """, (alert_id, f"team:{team}", Jsonb(rendered), f"{REVIEW_RULE}:{shape}"))
    notification_id, = await cursor.fetchone()
    await notify(conn, "alerts", "alert.created", id=alert_id, team_id=team, status="open",
                 severity="unknown", priority="P3", occurrence_count=count)
    await notify(conn, "notifications", "notification.updated", id=str(notification_id),
                 alert_id=alert_id, team_id=team, state="delivered")
    return team


async def register_unknowns(conn, events: list[CanonicalEvent]):
    groups = defaultdict(list)
    for event in events:
        groups[fingerprint(event.raw)].append(event)
    # Concurrent batches lock their issue rows in the same order.
    for shape in sorted(groups):
        group = groups[shape]
        sample = group[0]
        first = min(e.received_at for e in group)
        last = max(e.received_at for e in group)
        await conn.execute("UPDATE events SET review_fingerprint=%s WHERE id=ANY(%s)", (shape, [e.id for e in group]))
        cursor = await conn.execute("""
            INSERT INTO schema_issues (fingerprint, source_id, sample_event_id, sample_fields, event_count,
                                       first_seen_at, last_seen_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (fingerprint) DO NOTHING RETURNING fingerprint
        """, (shape, sample.source_id, sample.id, Jsonb(sample.fields), len(group), first, last))
        if await cursor.fetchone():
            team = await _open_notice(conn, shape, sample, len(group), first, last)
        else:
            cursor = await conn.execute("""
                WITH prior AS (SELECT status AS old_status FROM schema_issues WHERE fingerprint=%s FOR UPDATE)
                UPDATE schema_issues SET event_count=event_count+%s, first_seen_at=least(first_seen_at,%s),
                    last_seen_at=greatest(last_seen_at,%s),
                    sample_event_id=CASE WHEN status='resolved' THEN %s ELSE sample_event_id END,
                    source_id=CASE WHEN status='resolved' THEN %s ELSE source_id END,
                    sample_fields=CASE WHEN status='resolved' THEN %s ELSE sample_fields END,
                    resolved_schema_id=CASE WHEN status='resolved' THEN NULL ELSE resolved_schema_id END,
                    status=CASE WHEN status='resolved' THEN 'open' ELSE status END
                FROM prior WHERE fingerprint=%s RETURNING status, sample_event_id, prior.old_status
            """, (shape, len(group), first, last, sample.id, sample.source_id, Jsonb(sample.fields), shape))
            status, sample_id, old_status = await cursor.fetchone()
            if status == "ignored":
                continue
            cursor = await conn.execute("""
                UPDATE alerts SET occurrence_count=occurrence_count+%s, last_seen_at=greatest(last_seen_at,%s),
                    representative_event_id=%s, status=CASE WHEN status='resolved' THEN 'open' ELSE status END,
                    resolved_at=NULL, resolved_by=NULL
                WHERE rule_id=%s AND alert_key=%s RETURNING id, team_id, status, occurrence_count
            """, (len(group), last, sample_id, REVIEW_RULE, shape))
            alert_id, team, alert_status, count = await cursor.fetchone()
            await notify(conn, "alerts", "alert.updated", id=alert_id, team_id=team,
                         status=alert_status, occurrence_count=count)
            if old_status != "resolved":
                continue
        await notify(conn, "schema_issues", "schema.issue", id=shape, fingerprint=shape,
                     source_id=sample.source_id, team_id=team, status="open")


async def index_legacy(conn):
    """Called under the registry write lock for rows created before migration 201."""
    while True:
        cursor = await conn.execute("""
            SELECT to_jsonb(e) FROM events e WHERE status='needs_review' AND review_fingerprint IS NULL
            ORDER BY id LIMIT 200 FOR UPDATE
        """)
        rows = await cursor.fetchall()
        if not rows:
            return
        await register_unknowns(conn, [CanonicalEvent.model_validate(row) for row, in rows])


async def refresh_issue(conn, shape, schema_id):
    # Different nested layouts (or different sources) can share top-level keys.
    cursor = await conn.execute("""
        SELECT id, source_id, fields FROM events WHERE review_fingerprint=%s AND status='needs_review'
        ORDER BY id LIMIT 1
    """, (shape,))
    remaining = await cursor.fetchone()
    if remaining:
        sample_id, source, fields = remaining
        await conn.execute("""
            UPDATE schema_issues SET sample_event_id=%s, source_id=%s, sample_fields=%s WHERE fingerprint=%s
        """, (sample_id, source, Jsonb(fields), shape))
        cursor = await conn.execute("""
            UPDATE alerts SET representative_event_id=%s WHERE rule_id=%s AND alert_key=%s RETURNING id, team_id, status
        """, (sample_id, REVIEW_RULE, shape))
        status = "open"
    else:
        await conn.execute("""
            UPDATE schema_issues SET status='resolved', resolved_schema_id=%s WHERE fingerprint=%s
        """, (schema_id, shape))
        cursor = await conn.execute("""
            UPDATE alerts SET status='resolved', resolved_by='system', resolved_at=now()
            WHERE rule_id=%s AND alert_key=%s RETURNING id, team_id, status
        """, (REVIEW_RULE, shape))
        status = "resolved"
    alert_id, team, alert_status = await cursor.fetchone()
    await notify(conn, "alerts", "alert.updated", id=alert_id, team_id=team, status=alert_status)
    await notify(conn, "schema_issues", "schema.issue", id=shape, fingerprint=shape, team_id=team, status=status)
