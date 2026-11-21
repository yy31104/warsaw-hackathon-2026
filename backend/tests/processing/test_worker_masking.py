import json

from alerting.config import files
from alerting.routing import plan
from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings, enqueue
from alerting.rules.worker import process_batch
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .notify import listen
from .test_golden import START


VALUE = "ACMESYNTHETIC98761234"
FIELDS = {"request.ref": VALUE, "request": {"ref": VALUE}, "$alias": VALUE}
RULE = {"id": "mask", "owner": "team:settlements", "match": {"field": "request.ref", "op": "exists"},
        "route": [{"to": "user:sam", "channels": ["phone", "sms", "email", "ui"]}],
        "present": {"title": "Value {{$alias}}", "body": "Check {{request.ref}}", "fields": ["request", "request.ref"]}}
SCHEMA = {"id": "synthetic", "sensitive": ["request.ref"]}
BOOK = {"id": "book", "rules": ["mask"], "content": "x" * 504 + VALUE, "format": "text"}


def test_pure_projection_masks_before_truncation_and_leaves_ui_flag():
    alert = {"id": 2, "rule_version": 1, "rule_id": "mask", "alert_key": "k", "status": "open",
             "severity": "error", "title": "x" * 116 + VALUE, "body": VALUE, "fields": FIELDS}
    routed = plan(alert, "created", RULE, FIELDS, files.directory(), [BOOK], {}, START,
                  sensitive_paths=SCHEMA["sensitive"])
    for intent in routed.intents:
        rendered = intent.rendered.model_dump(mode="json")
        external = intent.channel != "ui"
        assert rendered["meta"]["masked"] is external
        if external:
            assert "ACMESYNTHETIC" not in json.dumps(rendered)
            assert rendered["fields"]["request"] == {"ref": "****1234"}
            assert rendered["fields"]["$alias"] == "****1234"
            assert len(rendered["short"]) <= 160 and rendered["link"] in rendered["short"]
        else:
            assert rendered["fields"]["request.ref"] == VALUE
    assert alert["fields"] == FIELDS


async def test_worker_and_manual_escalation_share_sensitive_schema_policy(database):
    settings = Settings(Ruleset([RULE], [SCHEMA]), files.directory(), [BOOK])
    async with database.connection() as conn:
        await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity)
               values('mask','http','hash','{}',%s,'synthetic','error')""",
            (Jsonb({**FIELDS, "sensitive_paths": [], "masked": False}),),
        )
    assert await process_batch(database, settings) == 1
    async with database.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("select * from alerts")
        alert = await cur.fetchone()
        assert alert["fields"]["request.ref"] == VALUE
        # The existing runbook action loads its alert without joining schema policy.
        direct = plan(alert, "escalation", RULE, FIELDS, settings.directory, [BOOK], {}, START)
        await enqueue(cur, direct.intents, alert["team_id"])
        await cur.execute("select channel,kind,rendered from notifications")
        rows = await cur.fetchall()
    assert len(rows) == 8
    for row in rows:
        rendered = row["rendered"]
        assert rendered["meta"]["masked"] is (row["channel"] != "ui")
        if row["channel"] != "ui":
            assert "ACMESYNTHETIC" not in json.dumps(rendered)
            assert rendered["fields"]["request.ref"] == "****1234"
        else:
            assert VALUE in rendered["title"]


async def test_older_incident_uses_registry_policy_and_unknown_schema_sends_link_only(database):
    settings = Settings(Ruleset([RULE], [SCHEMA]), files.directory(), [BOOK])
    async with database.connection() as conn:
        await conn.execute(
            """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,schema_version,severity)
               values('mask','http','hash','{}',%s,'synthetic',1,'error')""", (Jsonb(FIELDS),),
        )
    assert await process_batch(database, settings) == 1
    async with database.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("update alerts set sensitive_paths=null returning *")
        old = await cur.fetchone()
        await cur.execute(
            """insert into config_objects(kind,id,version,spec,content_hash,origin)
               values('schema','synthetic',1,%s,'hash','ui')""", (Jsonb(SCHEMA),),
        )
        for known in (True, False):
            if not known:
                await cur.execute("update events set schema_id='missing-policy'")
            direct = plan(old, "escalation", RULE, FIELDS, settings.directory, [], {}, START)
            sms = next(i for i in direct.intents if i.channel == "sms")
            sms.dedup_key += f"-{known}"
            await enqueue(cur, [sms], old["team_id"])
            await cur.execute("select rendered from notifications where dedup_key=%s", (sms.dedup_key,))
            msg = (await cur.fetchone())["rendered"]
            assert msg["meta"]["masked"] and VALUE not in json.dumps(msg)
            if known:
                assert "****1234" in msg["short"]
            else:
                assert msg["fields"] == {} and msg["link"] in msg["short"]


async def test_alert_created_flags_sensitive_data_without_changing_counter_update_shape(database):
    cfg = Settings(Ruleset([RULE], [SCHEMA]), files.directory(), [BOOK])
    async with listen(database) as drain:
        for _ in range(2):
            async with database.connection() as conn:
                await conn.execute(
                    """insert into events(source_id,transport,payload_hash,raw,fields,schema_id,severity)
                       values('mask','http','hash','{}',%s,'synthetic','error')""", (Jsonb(FIELDS),),
                )
            assert await process_batch(database, cfg) == 1
        signals = [msg for channel, msg in await drain() if channel == "alerts"]
    first, repeat = signals
    assert first["type"] == "alert.created" and first["data"]["masked"] is True
    assert repeat["type"] == "alert.updated" and "masked" not in repeat["data"]
    assert VALUE not in json.dumps(signals)
