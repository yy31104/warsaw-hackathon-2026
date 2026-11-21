import pytest


RESPONSE_PATH = "/v1/stats/response"


@pytest.fixture
async def response_activity(inbox_pool):
    # All offsets share PostgreSQL's transaction timestamp. Recent activity sits
    # comfortably inside a two-minute window, with history half an hour earlier.
    async with inbox_pool.connection() as conn:
        await conn.execute("""
            INSERT INTO events (id, source_id, transport, payload_hash, raw, status, received_at)
            SELECT id, source, 'http', 'fixture-' || id, '{}', 'processed',
                   now() - received_ago * interval '1 second'
            FROM (VALUES
                (100, 'fixture', 100),
                (101, 'fixture',  61),
                (102, 'fixture',  90),
                (103, 'fixture',  80),
                (104, 'fixture',  75),
                (105, 'fixture', 1800),
                (106, 'canary',   65),
                (107, 'fixture',  65),
                (108, 'fixture', -600)
            ) AS sample(id, source, received_ago)
        """)
        # The no-action incident's representative event replaced its opening event.
        await conn.execute("""
            INSERT INTO alerts (id, rule_id, rule_version, alert_key, team_id, status,
                                representative_event_id, created_at, first_seen_at,
                                acked_at, resolved_at, resolve_reason)
            SELECT id, rule, 1, alert_key, team, status, event_id,
                   now() - created_ago * interval '1 second',
                   now() - seen_ago * interval '1 second',
                   now() - acked_ago * interval '1 second',
                   now() - resolved_ago * interval '1 second', resolve_reason
            FROM (VALUES
                (1, 'fixture', 'no-action', 'alpha', 'resolved',     101,  95,   96, 76,   36, 'no_action_needed'),
                (2, 'fixture', 'coalesced', 'beta',  'acknowledged', 102,  85, NULL, 55, NULL, NULL),
                (3, 'fixture', 'team-only', 'quiet', 'open',         103,  75,   77, NULL, NULL, NULL),
                (4, 'fixture', 'manual',    'beta',  'resolved',     104,  70,   71, NULL, 26, 'manual'),
                (5, 'fixture', 'old',       'alpha', 'open',         105, 1795, 1796, NULL, NULL, NULL),
                (6, 'delivery-canary', 'pulse', 'alpha', 'resolved', 106,  60,   61, 51,   50, 'no_action_needed'),
                (7, 'fixture', 'future',    'beta',  'resolved',     108, -605, -603, NULL, -655, 'manual')
            ) AS sample(id, rule, alert_key, team, status, event_id,
                        created_ago, seen_ago, acked_ago, resolved_ago, resolve_reason)
        """)
        await conn.execute("""
            INSERT INTO decisions (event_id, alert_id, outcome, reason, created_at)
            SELECT event_id, alert_id, outcome, jsonb_build_object('transition', transition),
                   now() - created_ago * interval '1 second'
            FROM (VALUES
                (100, 1, 'NEW_ALERT',      'created',    95),
                (101, 1, 'EXISTING_ALERT', 'occurrence', 60),
                (105, 5, 'UNROUTED',       'created',  1795),
                (105, 5, 'UNROUTED',       'occurrence', 31),
                (106, 6, 'UNROUTED',       'created',    60),
                (107, NULL, 'UNROUTED',    NULL,         62),
                (108, 7, 'UNROUTED',       'created',  -605)
            ) AS sample(event_id, alert_id, outcome, transition, created_ago)
        """)
        await conn.execute("""
            INSERT INTO notifications (alert_id, kind, recipient, channel, rendered, state,
                                       dedup_key, created_at, delivered_at)
            VALUES (1, 'merged', 'sam', 'sms', '{}', 'delivered', 'shared-sms',
                    now() - interval '72 seconds', now() - interval '70 seconds')
        """)
        await conn.execute("""
            INSERT INTO notifications (alert_id, kind, recipient, channel, rendered, state,
                                       merged_into, created_at, delivered_at)
            SELECT sample.alert_id, sample.kind, sample.recipient, sample.channel, '{}', sample.state,
                   parent.id, now() - created_ago * interval '1 second',
                   now() - delivered_ago * interval '1 second'
            FROM (VALUES
                (1, 'initial',    'team:alpha', 'ui',    'delivered', NULL,        94, 94),
                (1, 'initial',    'sam',        'ui',    'delivered', NULL,        90, 90),
                (1, 'initial',    'ignored',    'sms',   'retrying',  NULL,        93, 93),
                (1, 'initial',    'unconfirmed','phone','delivered', NULL,        93, NULL),
                (1, 'canary',     'canary',     'sms',   'delivered', NULL,        93, 92),
                (1, 'escalation', 'future',     'sms',   'delivered', NULL,        89, -600),
                (2, 'initial',    'sam',        'sms',   'delivered', 'shared-sms',72, 70),
                (2, 'escalation', 'rita',       'phone', 'delivered', NULL,        60, 55),
                (3, 'initial',    'team:quiet', 'ui',    'delivered', NULL,        73, 73),
                (3, 'initial',    'pending',    'sms',   'pending',   NULL,        74, NULL),
                (4, 'initial',    'team:beta',  'ui',    'delivered', NULL,        69, 69),
                (5, 'escalation', 'sam',        'phone', 'delivered', NULL,       180, 30),
                (5, 'initial',    'former',     'sms',   'delivered', NULL,      1700, 1700),
                (5, 'escalation', 'anna',       'phone', 'delivered', NULL,        60, 29),
                (6, 'canary',     'canary',     'sms',   'delivered', NULL,        59, 58)
            ) AS sample(alert_id, kind, recipient, channel, state, parent_key, created_ago, delivered_ago)
            LEFT JOIN notifications parent ON parent.dedup_key = sample.parent_key
        """)
        await conn.execute("""
            INSERT INTO delivery_attempts (notification_id, attempt, outcome)
            SELECT id, step.attempt, step.outcome FROM notifications
            CROSS JOIN (VALUES (1, 'retryable'), (2, 'delivered')) AS step(attempt, outcome)
            WHERE dedup_key = 'shared-sms'
        """)


async def test_response_stats_match_the_windowed_database_samples(client, inbox_pool, response_activity):
    # Check the per-alert samples independently in SQL, using the known opening
    # event of this fixture. The endpoint must recover that event from decisions.
    async with inbox_pool.connection() as conn:
        rows = await conn.execute("""
            SELECT a.alert_key,
                   extract(epoch FROM (
                       min(n.delivered_at) FILTER (WHERE n.recipient NOT LIKE 'team:%') - e.received_at
                   )),
                   extract(epoch FROM a.acked_at - coalesce(a.first_seen_at, a.created_at)),
                   extract(epoch FROM a.resolved_at - coalesce(a.first_seen_at, a.created_at)),
                   count(n.id), a.resolve_reason
            FROM alerts a
            JOIN events e ON e.id = CASE WHEN a.alert_key = 'no-action' THEN 100 ELSE a.representative_event_id END
            LEFT JOIN notifications n ON n.alert_id = a.id AND n.state = 'delivered' AND n.kind <> 'canary'
                 AND n.delivered_at BETWEEN now() - interval '2 minutes' AND now()
            WHERE e.source_id <> 'canary' AND a.created_at BETWEEN now() - interval '2 minutes' AND now()
            GROUP BY a.id, e.received_at
            ORDER BY a.alert_key
        """)
        assert await rows.fetchall() == [
            ("coalesced", 20, 30, None, 2, None),
            ("manual", None, None, 45, 1, "manual"),
            ("no-action", 10, 20, 60, 3, "no_action_needed"),
            ("team-only", None, None, None, 1, None),
        ]

    response = await client.get(RESPONSE_PATH, params={"window": "2m"})
    assert response.status_code == 200
    assert response.json() == {
        "window": "2m",
        "n_alerts": 4,
        "tta_p50_s": 15,
        "tta_p95_s": 19.5,
        "mtta_s": 25,
        "mttr_s": 52.5,
        "ack_rate": 0.5,
        "unrouted": 2,
        "notifications_per_alert": 1.75,
        "no_action_rate": 0.5,
        "interruptions": [
            {"user": "sam", "count": 2},
            {"user": "anna", "count": 1},
            {"user": "rita", "count": 1},
        ],
        "sample_counts": {
            "n_alerts": 4, "tta_p50_s": 2, "tta_p95_s": 2, "mtta_s": 2, "mttr_s": 2,
            "ack_rate": 4, "unrouted": 2, "notifications_per_alert": 4, "no_action_rate": 2,
            "interruptions": 4,
        },
    }

    blank_team = await client.get(RESPONSE_PATH, params={"window": "2m", "team": ""})
    assert blank_team.status_code == 200
    assert blank_team.json() == response.json()


async def test_response_stats_scope_metrics_to_the_requested_team(client, response_activity):
    response = await client.get(RESPONSE_PATH, params={"window": "2m", "team": "beta"})
    assert response.status_code == 200
    assert response.json() == {
        "window": "2m",
        "n_alerts": 2,
        "tta_p50_s": 20,
        "tta_p95_s": 20,
        "mtta_s": 30,
        "mttr_s": 45,
        "ack_rate": 0.5,
        "unrouted": 0,
        "notifications_per_alert": 1.5,
        "no_action_rate": 0,
        "interruptions": [{"user": "rita", "count": 1}, {"user": "sam", "count": 1}],
        "sample_counts": {
            "n_alerts": 2, "tta_p50_s": 1, "tta_p95_s": 1, "mtta_s": 1, "mttr_s": 1,
            "ack_rate": 2, "unrouted": 0, "notifications_per_alert": 2, "no_action_rate": 1,
            "interruptions": 2,
        },
    }

    quiet = await client.get(RESPONSE_PATH, params={"window": "2m", "team": "quiet"})
    assert quiet.status_code == 200
    result = quiet.json()
    assert result["n_alerts"] == result["notifications_per_alert"] == 1
    assert result["ack_rate"] == 0
    assert result["interruptions"] == []
    for metric in ("tta_p50_s", "tta_p95_s", "mtta_s", "mttr_s", "no_action_rate"):
        assert result[metric] is None
        assert result["sample_counts"][metric] == 0


async def test_response_stats_empty_window_and_validation(client):
    response = await client.get(RESPONSE_PATH)
    assert response.status_code == 200
    assert response.json() == {
        "window": "24h",
        "n_alerts": 0,
        "tta_p50_s": None,
        "tta_p95_s": None,
        "mtta_s": None,
        "mttr_s": None,
        "ack_rate": None,
        "unrouted": 0,
        "notifications_per_alert": None,
        "no_action_rate": None,
        "interruptions": [],
        "sample_counts": dict.fromkeys((
            "n_alerts", "tta_p50_s", "tta_p95_s", "mtta_s", "mttr_s", "ack_rate", "unrouted",
            "notifications_per_alert", "no_action_rate", "interruptions",
        ), 0),
    }
    for invalid in ("0m", "-2h", "1w", "10000d", ""):
        response = await client.get(RESPONSE_PATH, params={"window": invalid})
        assert response.status_code == 422


async def test_funnel_counts_incident_openings_separately_from_open_alerts(client, response_activity):
    for window, openings in (("2m", 5), ("1h", 6)):
        response = await client.get("/v1/stats/funnel", params={"window": window})
        assert response.status_code == 200
        result = response.json()
        assert result["window"] == window
        assert result["incidents_opened"] == openings
        assert result["alerts_open"] == 3
