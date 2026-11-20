from datetime import datetime

import pytest
from alerting.clock import FixedClock
from alerting.config import files
from alerting.routing.directory import preferences
from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch
from psycopg.types.json import Jsonb


# 08:00–20:00 Warsaw time is 06:00–18:00 UTC after the spring change,
# and 07:00–19:00 UTC after the autumn change.
DAYTIME = [
    ("2026-03-29T05:59:00Z", "email"), ("2026-03-29T06:00:00Z", "sms"),
    ("2026-03-29T17:59:00Z", "sms"), ("2026-03-29T18:00:00Z", "email"),
    ("2026-10-25T06:59:00Z", "email"), ("2026-10-25T07:00:00Z", "sms"),
    ("2026-10-25T18:59:00Z", "sms"), ("2026-10-25T19:00:00Z", "email"),
]


@pytest.mark.parametrize(("instant", "channel"), [
    ("2026-03-29T00:59:00Z", "email"),  # 01:59, then Warsaw jumps to 03:00
    ("2026-03-29T01:00:00Z", "email"),
    ("2026-10-25T00:30:00Z", "sms"),   # First 02:30
    ("2026-10-25T01:30:00Z", "sms"),   # Repeated 02:30
    ("2026-10-25T02:00:00Z", "email"),
])
def test_skipped_and_repeated_hour(instant, channel):
    user = {"timezone": "Europe/Warsaw", "preferences": [
        {"when": "02:00-03:00", "channels": ["sms"]}, {"channels": ["email"]},
    ]}
    assert preferences(user, {}, {}, "critical", FixedClock(datetime.fromisoformat(instant))) == [channel]


async def test_warsaw_working_hours_through_worker(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    rule = {"id": "dst", "owner": "team:payments", "match": {"field": "case", "op": "exists"},
            "group": {"by": ["case"]}, "route": [{"to": "user:anna", "channels": "preferences"}]}
    settings = Settings(Ruleset([rule]), files.directory(), [])
    async with database.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """insert into events(source_id,transport,payload_hash,raw,fields,trace,severity,lane)
               values('dst','http','hash','{}',%s,%s,'critical','critical')""",
            [(Jsonb({"case": instant}), Jsonb({"demo_clock": instant})) for instant, _ in DAYTIME],
        )
    assert await process_batch(database, settings, lane="critical") == len(DAYTIME)
    async with database.connection() as conn:
        rows = await (await conn.execute(
            """select e.fields->>'case',n.channel from notifications n join alerts a on a.id=n.alert_id
               join events e on e.id=a.representative_event_id where n.recipient='anna' order by e.id"""
        )).fetchall()
    assert rows == DAYTIME
