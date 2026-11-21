"""Black-box runner regressions. All services here are HTTP fakes."""
import json
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from loadtest import golden_runner as golden


def run(cases, handler):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return golden.Runner(client, api="http://api", telecom="http://telecom",
                             mailpit="http://mail", timeout=0).run(
            {"clock_default": "2026-10-23T10:00:00+02:00", "cases": cases})


def response(data, status=200):
    return httpx.Response(status, json=data)


def test_missing_reset_records_every_real_case_and_preserves_other_gates():
    suite = yaml.safe_load((ROOT / "fixtures/golden/expectations.yaml").read_text())
    calls = []

    def unavailable(request):
        calls.append((request.method, request.url.path))
        return response({"detail": "Not Found"}, 404)

    results = run(suite["cases"], unavailable)
    assert [r.case_id for r in results] == [c["id"] for c in suite["cases"]]
    assert all(r.status == "NOT_RUN" and "404" in r.detail and "/v1/debug/reset" in r.detail
               for r in results)
    assert calls == [("POST", "/v1/debug/reset")] * len(results)
    previous = "# Gates\n\n## G0\nword-for-word\n\n## Golden run\nold table\n\n## G4\nkeep\n"
    updated = golden.replace_report(previous, golden.table(results))
    assert updated.startswith("# Gates\n\n## G0\nword-for-word\n\n")
    assert updated.endswith("## G4\nkeep\n")
    assert golden.replace_report(updated, golden.table(results)) == updated
    assert golden.table(results).count("| NOT_RUN |") == len(results)


def test_no_broker_is_default_and_never_sends_bus_fixture_to_a_webhook():
    def only_reset(request):
        assert request.url.path == "/v1/debug/reset"
        return response({})

    cases = [{"id": "bus", "send": {"source": "trade-bus",
              "file": "fixtures/events/trade_rejected_v2.json"}, "expect": {"no_alert_for_rule": "r"}}]
    result, = run(cases, only_reset)
    assert result.status == "NOT_RUN" and "broker" in result.detail.lower()


def test_negative_never_passes_with_pending_or_quarantined_input():
    for status in ("pending", "quarantined"):
        def handler(request):
            path = request.url.path
            if path == "/v1/debug/reset":
                return response({})
            if request.method == "POST":
                assert request.headers["x-api-key"] == "demo-alertmanager-key"
                assert request.headers["x-demo-clock"].endswith("+02:00")
                assert int(request.headers["x-gen-sent-at"]) > 0
                return response({"accepted": 1, "duplicates": 0, "event_ids": [17]}, 202)
            if path in ("/v1/events/17/explain", "/v1/events/17"):
                return response({"id": 17, "status": status, "decisions": [], "outcome": "ACCEPTED"})
            assert path == "/v1/alerts"
            return response({"items": []})

        case = {"id": "negative", "send": {"source": "alertmanager", "body": {"sample": 1}},
                "expect": {"no_alert_for_rule": "r"}}
        result, = run([case], handler)
        assert result.status == "FAIL" and status in result.detail


def test_verification_submits_both_transforms_and_checks_the_counterfactual():
    suite = yaml.safe_load((ROOT / "fixtures/golden/expectations.yaml").read_text())
    case = next(c for c in suite["cases"] if c["id"] == "verify-unit-change")
    requests = []

    def handler(request):
        if request.url.path == "/v1/debug/reset":
            return response({})
        assert request.url.path == "/v1/schemas/verify"
        body = json.loads(request.content)
        assert [s["event"]["payload"]["notional"] for s in body["samples"]] == [25_000_000, 2_500_000_000]
        assert all("event_file" not in s and "mutate" not in s for s in body["samples"])
        requests.append(body["schema"])
        return response({"results": [{"pass": True, "deciding_fields": ["payload.notional"],
                                      "recipients": [], "channels": []},
                                     {"pass": True, "deciding_fields": ["payload.notional"],
                                      "recipients": ["rita", "team:rates"], "channels": ["email", "ui"]}]})

    result, = run([case], handler)
    assert result.status == "FAIL" and "sample_0" in result.detail
    assert "transforms" in requests[0] and "transforms" not in requests[1]


def test_after_replays_parent_inputs_in_fresh_state_and_records_real_mismatch():
    calls = []

    def handler(req):
        path = req.url.path
        calls.append((req.method, path))
        if path == "/v1/debug/reset":
            return response({})
        if path == "/v1/events/alertmanager":
            return response({"accepted": 1, "duplicates": 0, "event_ids": [1]}, 202)
        if path in ("/v1/events/1", "/v1/events/1/explain"):
            return response({"id": 1, "status": "processed", "outcome": "NEW_ALERT", "decisions": []})
        assert path == "/v1/alerts"
        return response({"items": [{"id": 1, "rule_id": "r", "title": "actual", "fields": {"key": 1}}]})

    results = run([{"id": "parent", "send": {"source": "alertmanager", "body": {"sample": 1}},
                    "expect": {"alert": {"rule": "r", "fields_shown": ["key"]}}},
                   {"id": "child", "after": "parent", "expect": {"alert": {"rule": "r", "title_equals": "wrong"}}}],
                  handler)
    assert [r.status for r in results] == ["PASS", "FAIL"]
    assert "actual" in results[1].detail and "wrong" in results[1].detail
    assert calls.count(("POST", "/v1/debug/reset")) == calls.count(("POST", "/v1/events/alertmanager")) == 2


def test_email_pass_requires_a_new_receipt_at_correct_address_with_same_notification_id():
    for address, key, expected in [("anna@example.com", "n1", "PASS"),
                                   ("other@example.com", "n1", "FAIL"),
                                   ("anna@example.com", "different", "FAIL")]:
        sent = False

        def handler(req):
            nonlocal sent
            path = req.url.path
            if path == "/v1/debug/reset":
                return response({})
            if path == "/v1/events/alertmanager":
                sent = True
                return response({"accepted": 1, "duplicates": 0, "event_ids": [3]}, 202)
            if path in ("/v1/events/3/explain", "/v1/events/3"):
                return response({"id": 3, "status": "processed", "outcome": "NEW_ALERT", "decisions": []})
            if path == "/v1/notifications":
                return response({"items": [{"id": "n1", "recipient": "anna", "channel": "email", "state": "delivered"}]})
            if path == "/api/v1/messages":
                old = {"ID": "old", "To": [{"Address": "anna@example.com"}]}
                new = {"ID": "new", "To": [{"Address": address}]}
                return response({"messages": [new, old] if sent else [old], "total": 2 if sent else 1})
            assert path in ("/api/v1/message/old/headers", "/api/v1/message/new/headers")
            return response({"X-Notification-Id": ["n1" if "/old/" in path else key]})

        result, = run([{"id": "mail", "send": {"source": "alertmanager", "body": {"sample": 1}},
                       "expect": {"notifications": [{"recipient": "anna", "channel": "email"}]}}], handler)
        assert result.status == expected, result


def test_unrecognised_expectation_fails_closed():
    results = run([{"id": "typo", "expect": {"mystery": True}}],
                  lambda request: response({}))
    assert results[0].status == "FAIL" and "mystery" in results[0].detail


def test_503_from_rule_service_is_a_measured_failure():
    def handler(request):
        if request.url.path == "/v1/debug/reset":
            return response({})
        return response({"detail": "evaluation failed"}, 503)

    result, = run([{"id": "unhealthy", "request": {"method": "POST", "path": "/v1/rules/check"},
                    "body": {"rule_id": "r"}, "expect": {"violations": 0}}], handler)
    assert result.status == "FAIL" and "503" in result.detail


def test_log_ingress_requires_an_existing_watched_file_and_never_opens_a_database(tmp_path):
    events = []
    target = tmp_path / "watched.log"
    fixture = ROOT / "fixtures/events/pricing-engine.log"
    case = {"id": "log", "send": {"source": "pricing-logs", "file": "fixtures/events/pricing-engine.log"},
            "expect": {"no_alert_for_rule": "absent"}}

    def handler(request):
        path = request.url.path
        if path == "/v1/debug/reset":
            return response({})
        if path == "/v1/events":
            for i, _ in enumerate(target.read_text().splitlines() if target.exists() else []):
                if i + 1 not in events:
                    events.append(i + 1)
            return response({"items": [{"id": i} for i in events]})
        if path.endswith("/explain"):
            return response({"id": int(path.split("/")[3]), "status": "processed", "outcome": "NO_MATCH"})
        assert path == "/v1/alerts"
        return response({"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runner = golden.Runner(client, api="http://api", telecom="http://t", mailpit="http://m",
                               log_input=target, timeout=0)
        suite = {"clock_default": "2026-10-23T10:00:00+02:00", "cases": [case]}
        result, = runner.run(suite)
        assert result.status == "NOT_RUN" and "--log-input" in result.detail
        assert not target.exists()
        target.touch()
        result, = runner.run(suite)
        assert result.status == "PASS" and target.read_bytes() == fixture.read_bytes()


def test_counterfactual_still_runs_after_an_observed_positive_failure():
    suite = yaml.safe_load((ROOT / "fixtures/golden/expectations.yaml").read_text())
    case = next(c for c in suite["cases"] if c["id"] == "verify-unit-change")
    transforms = []

    def handler(request):
        if request.url.path == "/v1/debug/reset":
            return response({})
        transforms.append("transforms" in json.loads(request.content)["schema"])
        return response({"results": [{"pass": False, "deciding_fields": []}, {"pass": False}]})

    result, = run([case], handler)
    assert result.status == "FAIL"
    assert transforms == [True, False] and "unit_warning" in result.detail and "all_pass" in result.detail


def test_accepted_explanation_alone_cannot_prove_a_negative():
    def handler(req):
        p = req.url.path
        if p == "/v1/debug/reset":
            return response({})
        if p == "/v1/events/alertmanager":
            return response({"accepted": 1, "duplicates": 0, "event_ids": [9]}, 202)
        if p.endswith("/explain"):
            return response({"id": 9, "status": "processed", "outcome": "ACCEPTED"})
        return response({"items": []})

    case = {"id": "quiet", "send": {"source": "alertmanager", "body": {"sample": True}},
            "expect": {"no_alert_for_rule": "r"}}
    result, = run([case], handler)
    assert result.status == "FAIL" and "ACCEPTED" in result.detail


def test_report_does_not_replace_a_differently_named_section():
    previous = "# Gates\n\n## Golden runner maintenance\nPreserve this.\n"
    updated = golden.replace_report(previous, "new")
    assert updated.startswith(previous) and updated.endswith("## Golden run\n\nnew\n")
