from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from alerting.clock import SYSTEM
from alerting.rules.scheduler import tick


OPENED = datetime(2026, 1, 12, 6, 59, 30, tzinfo=UTC)


async def open_alert(db, **rule_options):
    team = {
        "id": "escalation-desk", "members": ["anna", "lee", "sam"], "approvers": ["lee", "sam"],
        "oncall": {"primary": "sam", "secondary": "anna"}, "escalate_after": "2m",
    }
    rule = {
        "id": "inventory-page", "owner": "team:escalation-desk", "match": {"inventory": "low"},
        "route": [{"to": "oncall:escalation-desk", "channels": "preferences"}], **rule_options,
    }
    async with db.connection() as conn:
        for kind, obj in (("team", team), ("rule", rule)):
            await conn.execute(
                """insert into config_objects(kind,id,version,spec,content_hash,origin)
                   values (%s,%s,1,%s,'synthetic','ui')""", (kind, obj["id"], Jsonb(obj)),
            )
        event_id, = await (await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,status)
               values ('inventory','http','sample','{}','{"inventory":"low"}','processed') returning id"""
        )).fetchone()
        alert_id, = await (await conn.execute(
            """insert into alerts(rule_id,rule_version,alert_key,team_id,severity,created_at,first_seen_at,
                      last_seen_at,last_notified_at,occurrence_count,representative_event_id)
               values (%s,1,'low',%s,'critical',%s,%s,%s,%s,1,%s) returning id""",
            (rule["id"], team["id"], OPENED, OPENED - timedelta(days=1), OPENED, OPENED, event_id),
        )).fetchone()
    return f"/v1/alerts/{alert_id}"


async def next_step(api, url):
    response = await api.get(url)
    assert response.status_code == 200, response.text
    value = response.json()["next_escalation"]
    if value is not None:
        value["at"] = datetime.fromisoformat(value["at"])
    return value


async def test_detail_predicts_both_scheduler_steps_without_reserving_deliveries(database, api, monkeypatch):
    url = await open_alert(database, escalate_after="1m", throttle={"per_recipient": 3, "per": "1h"})
    now = OPENED
    monkeypatch.setattr(SYSTEM, "now", lambda: now)
    first = {"at": OPENED + timedelta(minutes=1), "level": "secondary", "targets": ["anna"], "channels": ["sms"]}
    # Anna's critical preference switches from email to SMS between opening and the first deadline.
    assert await next_step(api, url) == first
    assert await next_step(api, url) == first
    async with database.connection() as conn:
        assert await (await conn.execute(
            "select (select count(*) from delivery_windows),(select count(*) from notifications)"
        )).fetchone() == (0, 0)

    now = first["at"] - timedelta(microseconds=1)
    assert await tick(database) == 0
    now = first["at"]
    assert await tick(database) == 1
    second = {
        "at": OPENED + timedelta(minutes=2), "level": "approvers", "targets": ["lee", "sam"],
        "channels": ["phone", "sms"],
    }
    assert await next_step(api, url) == second
    now = second["at"]
    assert await tick(database) == 1
    assert await next_step(api, url) is None
    async with database.connection() as conn:
        assert await (await conn.execute(
            """select rendered->'meta'->>'escalation_step',recipient,channel from notifications
               where kind='escalation' order by recipient,channel"""
        )).fetchall() == [
            ("secondary", "anna", "sms"), ("approvers", "lee", "phone"), ("approvers", "lee", "sms"),
            ("approvers", "sam", "phone"), ("approvers", "sam", "sms"),
        ]


@pytest.mark.parametrize("action", ["ack", "resolve"])
async def test_closing_or_acknowledging_cancels_the_advertised_team_timer(database, api, monkeypatch, action):
    url = await open_alert(database)
    now = OPENED
    monkeypatch.setattr(SYSTEM, "now", lambda: now)
    step = await next_step(api, url)
    assert step["at"] == OPENED + timedelta(minutes=2)
    assert (await api.post(f"{url}/{action}", headers={"X-User": "sam"})).status_code == 200
    assert await next_step(api, url) is None
    now = step["at"]
    assert await tick(database) == 0


async def test_missing_rule_or_absent_escalation_policy_has_no_next_step(database, api):
    url = await open_alert(database, escalate_after=None)
    assert await next_step(api, url) is None
    async with database.connection() as conn:
        await conn.execute("update config_objects set active=false where kind='rule' and id='inventory-page'")
    assert await next_step(api, url) is None


async def test_silence_defers_the_next_step_until_expiry(database, api, monkeypatch):
    url = await open_alert(database, escalate_after="1m")
    now = OPENED
    monkeypatch.setattr(SYSTEM, "now", lambda: now)
    release = OPENED + timedelta(minutes=5)
    async with database.connection() as conn:
        await conn.execute(
            """insert into silences(team_id,matcher,include_critical,created_by,created_at,expires_at)
               values ('escalation-desk','{"inventory":"low"}',true,'sam',%s,%s)""", (OPENED, release),
        )
    assert (await next_step(api, url))["at"] == release
    now = OPENED + timedelta(minutes=1)
    await tick(database)
    async with database.connection() as conn:
        assert await (await conn.execute("select escalation_stage from alerts")).fetchone() == (0,)
    now = release
    assert (await next_step(api, url))["at"] == release
    assert await tick(database) == 1


async def test_timer_starts_with_the_first_eligible_occurrence_after_silence(database, api, monkeypatch):
    url = await open_alert(database, escalate_after="1m")
    now = OPENED + timedelta(minutes=6)
    monkeypatch.setattr(SYSTEM, "now", lambda: now)
    async with database.connection() as conn:
        await conn.execute("update alerts set last_notified_at=null,count_since_notify=0")
    assert await next_step(api, url) is None
    async with database.connection() as conn:
        await conn.execute(
            """insert into decisions(event_id,alert_id,rule_id,rule_version,outcome,reason,created_at)
               select representative_event_id,id,rule_id,rule_version,'MATCHED',%s,%s from alerts""",
            (Jsonb({"notification_cause": "first_eligible_after_silence", "eligible_at": OPENED.isoformat()}), now),
        )
        await conn.execute("update alerts set last_notified_at=%s,occurrence_count=2", (now,))
    due = now + timedelta(minutes=1)
    assert (await next_step(api, url))["at"] == due
    assert await tick(database) == 0
    now = due
    assert await tick(database) == 1


async def test_confirmed_recovery_precedes_escalation_at_the_same_deadline(database, api, monkeypatch):
    url = await open_alert(database, escalate_after="1m")
    now = OPENED
    monkeypatch.setattr(SYSTEM, "now", lambda: now)
    due = OPENED + timedelta(minutes=1)
    async with database.connection() as conn:
        await conn.execute(
            """update alerts set latest_episode='one',resolve_episode='one',resolve_event_id=representative_event_id,
               resolve_pending_at=%s,resolve_due_at=%s""", (OPENED, due),
        )
    assert await next_step(api, url) is None
    now = due
    assert await tick(database) == 1
    assert (await api.get(url)).json()["status"] == "resolved"
    async with database.connection() as conn:
        assert await (await conn.execute("select count(*) from notifications")).fetchone() == (0,)
