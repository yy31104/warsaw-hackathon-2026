import pytest


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


async def test_debug_reset_is_demo_only_and_preserves_config(client, activity, inbox_pool, monkeypatch):
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            INSERT INTO config_objects (kind,id,version,spec,content_hash,origin)
            VALUES ('rule','repo-rule',1,'{}','hash','file'), ('rule','demo-rule',1,'{}','other','ui')
        """)
    monkeypatch.setenv("DEMO_MODE", "0")
    assert (await client.post("/v1/debug/reset")).status_code == 404
    assert (await client.get("/v1/stats/funnel")).json()["accepted"] == 4
    monkeypatch.setenv("DEMO_MODE", "1")
    assert (await client.post("/v1/debug/reset")).json() == {"status": "reset"}
    assert (await client.get("/v1/stats/funnel")).json()["accepted"] == 0
    async with inbox_pool.connection() as conn:
        rows = await conn.execute("SELECT id FROM config_objects ORDER BY id")
        assert await rows.fetchall() == [("demo-rule",), ("repo-rule",)]
