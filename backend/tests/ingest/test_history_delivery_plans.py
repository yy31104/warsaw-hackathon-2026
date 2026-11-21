from datetime import UTC, datetime, timedelta

from alerting.config import files
from alerting.contracts import CanonicalEvent
from alerting.simulate.history import preview_history, replay_history


ORIGIN = datetime(2026, 11, 20, 7, tzinfo=UTC)
RULE = {
    "id": "inventory-replay-detail",
    "owner": "team:rates",
    "match": {"inventory.state": "low"},
    "route": [{"to": "user:sam", "channels": ["sms"]}],
    "group": {"by": ["inventory.bin"], "cooldown": "10m"},
}


def at(minute):
    return ORIGIN + timedelta(minutes=minute)


def event(id, minute, *, bin="a", state="low", severity="warning"):
    return CanonicalEvent(
        id=id, source_id="inventory-test", transport="http", payload_hash=f"test-{id}",
        received_at=at(minute), severity=severity, raw={}, status="processed",
        fields={"$severity": severity, "inventory.state": state, "inventory.bin": bin},
    )


def test_preview_preserves_recipient_diff_while_showing_a_channel_change():
    draft = {**RULE, "route": [{"to": "user:sam", "channels": ["phone"]}]}
    report = preview_history([event(2, 1), event(1, 0)], RULE, draft, [], files.directory(), [])

    assert report["current"] == {"alerts": 1, "matched_events": 2, "notifications": {"sam/sms": 1}}
    assert report["draft"] == {"alerts": 1, "matched_events": 2, "notifications": {"sam/phone": 1}}
    assert report["events_with_recipient_loss"] == []
    assert report["event_delivery_plans"] == [
        {
            "event_id": 1,
            "current": [{"recipient": "sam", "channel": "sms", "planned_at": at(0).isoformat()}],
            "draft": [{"recipient": "sam", "channel": "phone", "planned_at": at(0).isoformat()}],
        },
        {"event_id": 2, "current": [], "draft": []},
    ]


def test_preview_preserves_episode_identity_and_the_historical_clock(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    sample = event(1, 120)
    sample = sample.model_copy(update={
        "schema_id": "inventory",
        "fields": {**sample.fields, "inventory.episode": "run-01"},
        "trace": {"demo_clock": "2026-11-20T08:03:00+01:00"},
    })
    schemas = [{"id": "inventory", "episode_key": "{{inventory.episode}}"}]

    report = preview_history([sample], RULE, RULE, schemas, files.directory(), [])
    assert report["event_delivery_plans"] == [{
        "event_id": 1,
        "episode": "run-01",
        "current": [{"recipient": "sam", "channel": "sms", "planned_at": at(3).isoformat()}],
        "draft": [{"recipient": "sam", "channel": "sms", "planned_at": at(3).isoformat()}],
    }]


def test_digest_attributes_the_shared_deadline_to_queued_events_only():
    digest = {**RULE, "group": {**RULE["group"], "mode": "digest", "every": "5m"}}
    events = [event(1, 0), event(2, 1, severity="critical"), event(3, 6, bin="b")]
    replay = replay_history(events, digest, [], files.directory(), [])

    delivery = ("sam", "sms", at(5))
    assert list(replay.planned_sends) == [delivery]
    assert dict(replay.planned_deliveries) == {1: {delivery}, 2: {delivery}}
    assert replay.critical_events == {1, 2}

    extended = replay_history(events, digest, [], files.directory(), [], through=at(11))
    assert extended.planned_deliveries[3] == {("sam", "sms", at(11))}


def test_throttle_credits_each_overflow_event_at_the_coalesced_send_time():
    throttled = {**RULE, "throttle": {"per_recipient": 1, "per": "10m"}}
    events = [event(i, i, bin=str(i)) for i in range(1, 4)]
    replay = replay_history(events, throttled, [], files.directory(), [])

    first_send = ("sam", "sms", at(1))
    overflow_send = ("sam", "sms", at(10))
    assert replay.summary()["notifications"] == {"sam/sms": 2}
    assert list(replay.planned_sends) == [first_send, overflow_send]
    assert dict(replay.planned_deliveries) == {1: {first_send}, 2: {overflow_send}, 3: {overflow_send}}


def test_distinct_sends_to_the_same_person_at_the_same_time_still_count_twice():
    history = replay_history([event(1, 0), event(2, 0, bin="b")], RULE, [], files.directory(), [])
    assert list(history.planned_sends) == [("sam", "sms", at(0)), ("sam", "sms", at(0))]


def test_missed_heartbeat_credits_the_beat_at_the_deadline():
    watch = {**RULE, "severity": "critical", "expect": {"every": "2m", "key": ["inventory.bin"]}}
    events = [event(1, 0), event(2, 4, state="ok")]
    replay = replay_history(events, watch, [], files.directory(), [])

    assert dict(replay.planned_deliveries) == {1: {("sam", "sms", at(2))}}
    assert replay.critical_events == {1}
