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
