from uuid import uuid4

import pytest
from pydantic import ValidationError

from alerting.config.validation import Preference, RuleDraft, RunbookDraft


def test_rule_draft_keeps_unseen_fields_and_removes_view_metadata():
    field = f"payload.{uuid4().hex}.0.flag"
    rule = RuleDraft.model_validate({
        "id": "holdout",
        "owner": "user:anna",
        "match": {"not": {"field": field, "op": "eq", "value": True}},
        "route": [{"to": "user:anna", "channels": "preferences"}],
        "version": 2,
        "origin": "file",
        "plain_english": "Old display string",
        "source": "llm",
    })
    assert rule.spec()["match"]["not"]["field"] == field
    assert rule.spec()["enabled"] is True
    assert not {"version", "origin", "plain_english", "source"} & rule.spec().keys()
    assert rule.version == 2


def test_invalid_rules_and_preferences_are_rejected():
    rule = dict(id="holdout", owner="user:anna", route=[{"to": "user:anna", "channels": ["ui"]}])
    with pytest.raises(ValidationError):
        RuleDraft.model_validate({**rule, "match": {"field": "x", "op": "regex", "value": "["}})
    with pytest.raises(ValidationError):
        RuleDraft.model_validate({**rule, "match": {}, "group": {"mode": "digest", "every": "0s"}})
    with pytest.raises(ValidationError):
        Preference.model_validate({"when": "25:00-03:00", "min_severity": "error", "channels": ["sms"]})
    assert Preference.model_validate({"when": "18:00-08:00", "min_severity": "error", "channels": ["sms"]}).when


@pytest.mark.parametrize(("content", "format"), [
    ("https://example.com/ops\n", "url"),
    ("# Diagnose\n1. Read the timeline\n", "markdown"),
    ("- [ ] Read the response\n", "checklist"),
    ("Ask the on-call engineer.\n", "text"),
    ("steps:\n  - action: an_unseen_action\n", "yaml-steps"),
])
def test_runbook_keeps_original_text_in_each_format(content, format):
    draft = RunbookDraft(id="holdout", content=content)
    assert draft.format == format
    assert draft.content == content


def test_runbook_cannot_downgrade_required_approval():
    with pytest.raises(ValidationError, match="requires approval"):
        RunbookDraft(id="holdout", content="steps:\n  - action: restart_service\n    approval: none")
