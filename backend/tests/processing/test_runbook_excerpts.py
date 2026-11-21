import json
from unittest.mock import MagicMock

import httpx
import pytest

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.delivery.providers.email import Email
from alerting.delivery.providers.telecom import Telecom
from alerting.delivery.worker import deliver_one
from alerting.ingest.normalize import normalize
from alerting.ingest.store import save_batch
from alerting.routing import plan
from alerting.rules.engine import Ruleset
from alerting.rules.storage import Settings
from alerting.rules.worker import process_batch


RULE = {
    "id": "excerpt-test", "owner": "team:settlements",
    "match": {"field": "workflowId", "op": "exists"},
    "route": [{"to": "user:sam", "channels": ["email", "ui", "sms", "phone"]}],
    "present": {"title": "Workflow stopped", "body": "Review the affected workflow."},
}
ACCOUNT = "SYNTHETIC-ACCOUNT-98761234"


def runbook(text):
    return {"id": "workflow-guide", "rules": [RULE["id"]], "format": "text", "content": text}


def messages(text, alert_fields, event_fields):
    alert = {
        "id": 42, "rule_id": RULE["id"], "rule_version": 1, "alert_key": "test",
        "status": "open", "severity": "error", "title": "Workflow stopped",
        "fields": alert_fields, "sensitive_paths": ["account.ref"],
    }
    routed = plan(alert, "created", RULE, event_fields, files.directory(), [runbook(text)], {}, SYSTEM)
    return {intent.channel.value: intent.rendered for intent in routed.intents}


@pytest.mark.parametrize("channel", ["email", "ui"])
def test_excerpt_combines_event_and_alert_fields_and_marks_missing_paths(channel):
    message = messages(
        "Restart {{workflowId}} in {{desk.name}}. Ticket {{ticket.id}}.",
        {"workflowId": "previous-workflow", "desk.name": "east"},
        {"workflowId": "wf-42"},
    )[channel]
    assert message.runbook_excerpt == "Restart wf-42 in east. Ticket ‹missing: ticket.id›."


def test_excerpt_marks_a_missing_workflow_id():
    routed = messages("{{workflowId}}", {}, {})
    assert routed["email"].runbook_excerpt == "‹missing: workflowId›"
    assert routed["ui"].runbook_excerpt == "‹missing: workflowId›"


def test_excerpt_masks_an_alert_only_value_before_the_512_character_cut():
    prefix = "x" * 500
    routed = messages(prefix + "{{account.ref}}|next", {"account.ref": ACCOUNT}, {"workflowId": "wf-42"})
    # The secret crosses character 512. Email has room for its mask and part of the suffix.
    assert routed["email"].runbook_excerpt == prefix + "****1234|nex"
    assert routed["ui"].runbook_excerpt == (prefix + ACCOUNT + "|next")[:512]


def test_excerpt_masks_a_sensitive_leaf_when_a_template_includes_its_parent():
    secret = "account\nref-1234"
    fields = {"account": {"ref": secret, "region": "east"}}
    routed = messages("Record {{account}}", {}, fields)
    assert routed["email"].runbook_excerpt == "Record " + str({"ref": "****1234", "region": "east"})
    assert routed["ui"].runbook_excerpt == "Record " + str(fields["account"])


@pytest.mark.parametrize("channel", ["sms", "phone"])
def test_telecom_envelopes_only_include_the_short_alert_and_link(channel):
    message = messages("Runbook instruction for {{workflowId}}", {}, {"workflowId": "wf-42"})[channel]
    assert message.runbook_excerpt is None
    assert message.short == message.body and message.link in message.short
    assert "Runbook instruction" not in message.short


async def test_worker_persists_and_providers_deliver_each_channels_excerpt(database, monkeypatch):
    schema = {
        "id": "excerpt-workflow", "name": "workflow.stopped", "detect": RULE["match"],
        "severity_default": "error", "sensitive": ["account.ref"],
    }
    book = runbook("Read workflow {{workflowId}} for account {{account.ref}}.")
    settings = Settings(Ruleset([RULE], [schema]), files.directory(), [book])
    event = normalize({"workflowId": "wf-42", "account": {"ref": ACCOUNT}},
                      {"id": "excerpt-test", "transport": "http"}, [schema], received_at=SYSTEM.now())
    async with database.connection() as conn:
        await save_batch(conn, [event])
    assert await process_batch(database, settings) == 1

    smtp = MagicMock()
    monkeypatch.setattr("smtplib.SMTP", smtp)
    telecom = {}

    def receive(request):
        telecom[request.url.path] = json.loads(request.content)["text"]
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        providers = {"email": Email(), "sms": Telecom("sms", client), "phone": Telecom("phone", client)}
        for channel in providers:
            assert await deliver_one(database, providers, [channel])

    async with database.connection() as conn:
        cursor = await conn.execute("select channel, rendered, state from notifications")
        rows = {channel: (message, state) for channel, message, state in await cursor.fetchall()}
    assert set(rows) == {"email", "ui", "sms", "phone"}
    assert all(state == "delivered" for _, state in rows.values())
    sent, = smtp.return_value.__enter__.return_value.send_message.call_args.args
    assert telecom == {"/sms": rows["sms"][0]["short"], "/call": rows["phone"][0]["short"]}
    assert all("Read workflow" not in text for text in telecom.values())
    assert all(rows[channel][0]["runbook_excerpt"] is None for channel in ("sms", "phone"))
    email_excerpt = "Read workflow wf-42 for account ****1234."
    assert email_excerpt in sent.get_content() and ACCOUNT not in sent.get_content()
    assert rows["email"][0]["runbook_excerpt"] == email_excerpt
    assert rows["ui"][0]["runbook_excerpt"] == f"Read workflow wf-42 for account {ACCOUNT}."
