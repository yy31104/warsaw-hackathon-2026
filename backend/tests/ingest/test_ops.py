import asyncio

import pytest
from psycopg import sql

from alerting.api.routes_ops import RESET_RUNTIME_TABLES


# Schemas, rules, teams, users and runbooks share the versioned config registry.
# File offsets survive each case so the tailer resumes after inputs that were already consumed.
PRESERVED_TABLES = {"config_objects", "config_state", "backend_file_rule_revisions", "log_tail_checkpoints"}


@pytest.fixture
async def activity(inbox_pool):
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            INSERT INTO events (source_id, transport, payload_hash, raw, status, received_at, processed_at)
            VALUES ('demo','http','one','{}','processed',now(),now()),
                   ('demo','http','older','{}','processed',now()-interval '5 minutes',now()-interval '5 minutes'),
                   ('demo','http','unknown','{}','needs_review',now(),NULL),
                   ('demo','http','pending','{}','pending',now(),NULL)
        """)
        for _ in range(2):
            await conn.execute("""
                INSERT INTO decisions (event_id, outcome, reason)
                SELECT id, 'NEW_ALERT', '{"matched":true}' FROM events WHERE payload_hash='one'
            """)
        await conn.execute("""
            INSERT INTO ingest_rejections (source_id,kind) VALUES ('demo','DUPLICATE'), ('demo','REJECTED')
        """)
        alert, = await (await conn.execute("""
            INSERT INTO alerts (rule_id,rule_version,alert_key,severity,lane,team_id)
            VALUES ('demo',1,'key','critical','critical','demo') RETURNING id
        """)).fetchone()
        await conn.execute("""
            INSERT INTO notifications (alert_id,kind,recipient,channel,rendered,state,created_at,delivered_at)
            VALUES (%s,'initial','demo','sms','{}','retrying',now()-interval '45 seconds',NULL),
                   (%s,'initial','demo','phone',
                    jsonb_build_object('meta',jsonb_build_object('gen_sent_at',1000)),
                    'delivered',now(),to_timestamp(1.25))
        """, (alert, alert))
        await conn.execute("""
            INSERT INTO delivery_attempts (notification_id, attempt, outcome)
            SELECT id, 2, 'retryable' FROM notifications WHERE channel='sms'
        """)


async def test_funnel_counts_distinct_events_and_uses_the_requested_window(client, activity, monkeypatch):
    monkeypatch.setenv("DELIVERY_TARGET_S", "30")
    response = await client.get("/v1/stats/funnel?window=1m")
    assert response.status_code == 200
    result = response.json()
    assert (result["window"], result["raw"], result["accepted"]) == ("1m", 5, 3)
    assert (result["duplicates"], result["rejected"], result["needs_review"], result["matched"]) == (1, 1, 1, 1)
    assert (result["alerts_open"], result["critical"], result["backlog"], result["processing_backlog"]) == (1, 1, 1, 1)
    assert result["at_risk"] and result["oldest_pending_s"] >= 44
    assert result["retries_5m"] == result["notifications"]["retrying"] == 1
    risk, = result["delivery"]
    assert risk["channel"] == "sms" and risk["lane"] == "critical" and risk["at_risk"]
    assert (await client.get("/v1/stats/funnel")).json()["accepted"] == 4
    assert (await client.get("/v1/stats/funnel?window=0m")).status_code == 422


async def test_metrics_reflect_outbox_and_inbox(client, activity, monkeypatch):
    monkeypatch.setenv("DELIVERY_TARGET_S", "30")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    for line in (
        "events_ingested_total 4",
        "events_processed_total 2",
        'events_backlog{lane="normal"} 1',
        'events_ingest_rejections_total{kind="DUPLICATE"} 1',
        'notifications_by_state{state="retrying"} 1',
        'delivery_at_risk{channel="sms",lane="critical"} 1',
        'delivery_attempts_total{channel="sms"} 1',
        'notification_confirmation_latency_seconds_bucket{le="0.1"} 0',
        'notification_confirmation_latency_seconds_bucket{le="0.25"} 1',
        'notification_confirmation_latency_seconds_bucket{le="+Inf"} 1',
        "notification_confirmation_latency_seconds_count 1",
        "notification_confirmation_latency_seconds_sum 0.25",
    ):
        assert line in response.text.splitlines()


async def test_debug_reset_classifies_every_migrated_table(inbox_pool):
    runtime = set(RESET_RUNTIME_TABLES)
    async with inbox_pool.connection() as conn:
        tables = await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        migrated = {name for name, in await tables.fetchall()}
        assert runtime.isdisjoint(PRESERVED_TABLES)
        assert migrated == runtime | PRESERVED_TABLES

        # A foreign key from a preserved table into runtime data would let CASCADE clear it.
        links = await conn.execute("""
            SELECT child.relname, parent.relname FROM pg_constraint fk
            JOIN pg_class child ON child.oid = fk.conrelid
            JOIN pg_class parent ON parent.oid = fk.confrelid
            WHERE fk.contype = 'f' AND child.relnamespace = current_schema()::regnamespace
              AND parent.relnamespace = child.relnamespace
        """)
        assert not [(child, parent) for child, parent in await links.fetchall()
                    if child in PRESERVED_TABLES and parent in runtime]


async def demo_table_rows(pool):
    rows = {}
    async with pool.connection() as conn:
        for table in (*RESET_RUNTIME_TABLES, *PRESERVED_TABLES):
            result = await conn.execute(sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY 1").format(sql.Identifier(table)))
            rows[table] = await result.fetchall()
    return rows


async def test_debug_reset_is_demo_only_and_preserves_config(client, activity, inbox_pool, monkeypatch):
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            INSERT INTO config_objects (kind,id,version,spec,content_hash,origin)
            VALUES ('rule','repo-rule',1,'{"name":"Heartbeat"}','hash','file'),
                   ('rule','demo-rule',2,'{"name":"Web edit"}','other','ui'),
                   ('schema','workflow',1,'{}','schema','file'), ('team','desk',1,'{}','team','api'),
                   ('runbook','retry',1,'{}','runbook','ui')
        """)
        await conn.execute("INSERT INTO backend_file_rule_revisions VALUES ('repo-rule','hash',1)")
        await conn.execute("""
            INSERT INTO heartbeat_watches (rule_id,alert_key,rule_version,group_values,last_seen_at,last_event_id,alert_id)
            SELECT a.rule_id,a.alert_key,a.rule_version,'{}',now(),e.id,a.id
            FROM events e CROSS JOIN alerts a WHERE e.payload_hash='one' AND a.rule_id='demo'
        """)
        await conn.execute("""
            INSERT INTO silences (team_id,matcher,created_by,created_at,expires_at)
            VALUES ('desk','{}','sam',now(),now()+interval '1 hour')
        """)
        await conn.execute("""
            INSERT INTO delivery_team_turns (channels,fallback,priority,team_id) VALUES (ARRAY['sms'],false,1,'desk')
        """)
        await conn.execute("INSERT INTO log_tail_checkpoints (source_id,path,byte_offset) VALUES ('log','demo.log',42)")
    before = await demo_table_rows(inbox_pool)
    assert len(before["heartbeat_watches"]) == 1
    monkeypatch.setenv("DEMO_MODE", "0")
    assert (await client.post("/v1/debug/reset")).status_code == 404
    assert await demo_table_rows(inbox_pool) == before
    assert (await client.get("/v1/stats/funnel")).json()["accepted"] == 4

    monkeypatch.setenv("DEMO_MODE", "1")
    after = {table: rows if table in PRESERVED_TABLES else [] for table, rows in before.items()}
    for _ in range(2):
        response = await client.post("/v1/debug/reset")
        assert response.status_code == 200
        assert response.json() == {"status": "reset"}
        assert await demo_table_rows(inbox_pool) == after
    assert (await client.get("/v1/stats/funnel")).json()["accepted"] == 0
    async with inbox_pool.connection() as conn:
        result = await conn.execute("""
            INSERT INTO events (source_id,transport,payload_hash,raw) VALUES ('demo','http','next','{}') RETURNING id
        """)
        assert await result.fetchone() == (1,)


async def test_demo_reset_survives_contention(client, inbox_pool, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    async with inbox_pool.connection() as conn:
        await conn.execute("LOCK TABLE events IN ROW SHARE MODE")
        request = asyncio.create_task(client.post("/v1/debug/reset"))
        # Hold the table past a single reset attempt's lock timeout.
        await asyncio.sleep(0.8)
        assert not request.done()
    assert (await request).json() == {"status": "reset"}
