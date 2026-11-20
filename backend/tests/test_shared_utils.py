"""Shared utilities used by every owner: templates and the config file reader."""
from alerting.config import files
from alerting.templates import render


def test_template_renders_rates_title_from_task():
    fields = {"payload.currency": "EUR", "payload.notional": 25000000, "payload.reason": "LIMIT_BREACH"}
    title = "Rejected {{payload.currency}} {{payload.notional}} — {{payload.reason}}"
    assert render(title, fields) == "Rejected EUR 25000000 — LIMIT_BREACH"


def test_template_missing_field_is_visible_not_fatal():
    assert render("x {{payload.reason}}", {}) == "x ‹missing: payload.reason›"


def test_template_nested_raw_lookup():
    assert render("{{error.code}}", {"error": {"code": "TIMEOUT"}}) == "TIMEOUT"


def test_config_files_load_task_examples():
    assert {r["id"] for r in files.rules()} == {
        "settlements-wf-failed", "pricing-warn-logs", "rates-big-rejects", "anna-payments-critical"}
    assert files.directory()["fallback_team"] == "platform"
    formats = {rb["id"]: rb["format"] for rb in files.runbooks()}
    assert formats == {"settlement-timeout": "yaml-steps", "payments-high-error-rate": "markdown", "pricing-latency": "text"}
    assert len(files.schemas()) == 4 and len(files.sources()) == 6


def test_describe_sample_keeps_json_valid_and_redacts_strings():
    import json

    from alerting.llm import describe_sample, redact

    raw = open("../fixtures/events/trade_rejected_v2.json").read()
    # The bug this guards against: redacting raw JSON text masks the epoch-ms timestamp and breaks the JSON.
    try:
        json.loads(redact(raw))
        broke = False
    except json.JSONDecodeError:
        broke = True
    assert broke, "redact() on raw JSON is expected to be unsafe; use describe_sample()"

    fields = {"meta.ts": 1792833245000, "payload.notional": 25000000, "contact.email": "anna@example.com",
              "note": "call +15550100001 now", "payload": {"desk": "rates"}}
    described = describe_sample(fields)
    json.loads(json.dumps(described))  # always valid JSON
    by_path = {d["path"]: d for d in described}
    assert by_path["meta.ts"] == {"path": "meta.ts", "type": "int", "example": 1792833245000}
    assert by_path["contact.email"]["example"] == "<redacted>"
    assert "+15550100001" not in by_path["note"]["example"]
    assert by_path["payload"]["example"] == "<dict len=1>"
