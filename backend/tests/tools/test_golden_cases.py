"""Exercise the multi-input fixtures against synthetic public responses, without a broker."""
import json
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from loadtest import golden_runner as golden

SUITE = yaml.safe_load(golden.GOLDEN.read_text())
CASES = {case["id"]: case for case in SUITE["cases"]}


def result_for(client, case, **kwargs):
    runner = golden.Runner(client, api="http://api", mailpit="http://mail", telecom="http://phone",
                           timeout=0, **kwargs)
    return runner.run({"cases": [case], "clock_default": SUITE["clock_default"]})[0]


@pytest.mark.parametrize("case_name,expected_total", [
    ("retransmission-dedup", 1), ("storm-500-one-alert", 500), ("poison-event-isolated", 4)])
def test_amqp_inputs_and_receiver_assertions_without_a_live_broker(case_name, expected_total, monkeypatch):
    inputs, seen, duplicates = [], set(), []
    notices = [{"id": f"n-{channel}", "channel": channel, "recipient": "sam", "state": "delivered"}
               for channel in ("phone", "sms")]

    async def fake_publish(queue, messages):
        assert queue == "events.workflow"
        for body, headers, key in messages:
            assert headers["X-Demo-Clock"] == SUITE["clock_default"]
            if key and key in seen:
                duplicates.append(key)
                continue
            seen.add(key)
            status = "quarantined" if headers.get("X-Demo-Fault") == "raise" else "processed"
            inputs.append({"id": len(inputs) + 1, "fields": body, "status": status})

    monkeypatch.setattr(golden, "publish", fake_publish)
    def service(request):
        path = request.url.path
        if path == "/v1/debug/reset":
            return httpx.Response(204)
        if path == "/v1/events":
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json={"items": inputs[offset:offset+100], "total": len(inputs)})
        if path.startswith("/v1/events/"):
            row = inputs[int(path.split("/")[3]) - 1]
            return httpx.Response(200, json={**row, "outcome": "QUARANTINED" if row["status"] == "quarantined" else "NEW_ALERT"})
        if path == "/v1/stats/funnel":
            return httpx.Response(200, json={"accepted": len(inputs), "duplicates": len(duplicates)})
        if path == "/v1/alerts":
            return httpx.Response(200, json={"items": [{"id": "a", "rule_id": "settlements-wf-failed",
                "occurrence_count": sum(i["status"] == "processed" for i in inputs), "status": "open"}] if inputs else []})
        if path == "/v1/notifications":
            return httpx.Response(200, json={"items": notices})
        assert path == "/messages"
        return httpx.Response(200, json={"items": [{"channel": n["channel"], "to": "+15550100002",
                               "idempotency_key": n["id"]} for n in notices] if inputs else []})

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        result = result_for(client, CASES[case_name], broker=True)
        assert result.status == "PASS", result
    assert len(inputs) == expected_total
    if case_name == "storm-500-one-alert":
        assert len({i["fields"]["workflowId"] for i in inputs}) == expected_total
    elif case_name == "poison-event-isolated":
        assert [i["status"] for i in inputs] == ["processed", "quarantined", "processed", "processed"]
        assert any(k and k.startswith("golden-worker-probe") for k in seen)
    else:
        assert duplicates == ["msg-dup-1"]


def test_failure_in_one_phase_keeps_later_recovery_observations():
    calls, events = [], []
    case = CASES["stale-recovery-keeps-newer-episode"]

    def service(req):
        path = req.url.path
        if path == "/v1/debug/reset":
            return httpx.Response(204)
        if path == "/v1/events/alertmanager":
            events.append(json.loads(req.content))
            return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "event_ids": [len(events)]})
        if path.endswith("/explain"):
            return httpx.Response(200, json={"status": "processed", "outcome": "NO_MATCH"})
        assert path == "/v1/alerts"
        calls.append(len(events))
        return httpx.Response(200, json={"items": [{"id": 1, "rule_id": "anna-payments-critical",
            "occurrence_count": 2, "status": "open", "resolved_by": None}]})

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        result = result_for(client, case)
    assert result.status == "FAIL" and "STALE_RESOLVE" in result.detail and "resolved" in result.detail
    assert calls == [3, 4]


def test_delivery_mismatch_survives_a_different_missing_receiver():
    case = {"id": "mixed", "expect": {"notifications": [
        {"recipient": "sam", "channel": "sms"}, {"recipient": "rita", "channel": "email"}]}}

    def service(req):
        if req.url.host == "phone":
            raise httpx.ConnectError("missing receiver", request=req)
        if req.url.path == "/v1/debug/reset":
            return httpx.Response(204)
        if req.url.host == "mail":
            return httpx.Response(200, json={"messages": [], "total": 0})
        return httpx.Response(200, json={"items": [{"id": "n", "recipient": "sam", "channel": "sms",
                                                      "state": "delivered"}]})

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        result = result_for(client, case)
    assert result.status == "FAIL" and "rita" in result.detail and "unavailable" in result.detail


@pytest.mark.parametrize("remove_receipt,drop_row", [(False, False), (True, False), (False, True)])
def test_outage_reconciliation_tracks_original_ids_and_restores_mode(remove_receipt, drop_row, monkeypatch):
    case = {
        "id": "outage", "setup": {"fake_telecom_mode": "down"},
        "send": {"source": "alertmanager", "body": {"sample": 1}},
        "then": {"wait_seconds": 20, "fake_telecom_mode": "ok", "wait_until_delivered_seconds": 0},
        "expect": CASES["channel-down-no-loss"]["expect"],
    }
    modes, waits = [], []
    monkeypatch.setattr(golden.time, "sleep", lambda delay: waits.append(delay))
    available = [{"id": "sms-id", "recipient": "sam", "channel": "sms"},
                 {"id": "phone-id", "recipient": "sam", "channel": "phone"}]

    def service(req):
        p = req.url.path
        if p == "/v1/debug/reset":
            return httpx.Response(204)
        if p == "/mode":
            if req.method == "POST":
                modes.append(json.loads(req.content)["mode"])
            return httpx.Response(200, json={"mode": modes[-1] if modes else "slow"})
        if p == "/v1/events/alertmanager":
            return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "event_ids": [1]})
        if p.endswith("/explain"):
            return httpx.Response(200, json={"status": "processed", "outcome": "NEW_ALERT"})
        if p == "/api/v1/messages":
            return httpx.Response(200, json={"messages": [], "total": 0})
        if p == "/v1/notifications":
            recovered = modes[-1] == "ok"
            notices = [dict(n, state="delivered" if recovered else "retrying", attempts=2) for n in available]
            return httpx.Response(200, json={"items": notices[:1] if recovered and drop_row else notices})
        assert p == "/messages"
        if not modes or modes[-1] != "ok":
            return httpx.Response(200, json={"items": []})
        sent = [{"idempotency_key": n["id"], "channel": n["channel"], "to": "+15550100002"} for n in available]
        return httpx.Response(200, json={"items": sent[:1] if remove_receipt else sent})

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        result = result_for(client, case)
    assert result.status == ("FAIL" if remove_receipt or drop_row else "PASS"), result
    assert modes == ["down", "ok", "slow"] and waits == [20]
    if result.status == "FAIL":
        assert "lost of 2" in result.detail


@pytest.mark.parametrize("case_id,report,status", [
    ("rule-safety-counterexample", {"violations": [{"problem": "route reaches nobody"}], "scenarios_checked": [1,2,3,4]}, "PASS"),
    ("rule-safety-counterexample", {"violations": [{"problem": "other"}], "scenarios_checked": [1,2,3]}, "FAIL"),
    ("rule-safety-ui-only-is-valid", {"violations": []}, "PASS"),
    ("rule-safety-ui-only-is-valid", {}, "FAIL"),
])
def test_rule_safety_observations(case_id, report, status):
    case = CASES[case_id]
    def service(req):
        if req.url.path == "/v1/debug/reset":
            return httpx.Response(204)
        assert req.url.path == "/v1/rules/check" and json.loads(req.content) == case["body"]
        return httpx.Response(200, json=report)

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        assert result_for(client, case).status == status


@pytest.mark.parametrize("wrong_audit_actor,expected", [(False, "PASS"), (True, "FAIL")])
def test_approve_uses_actual_request_actor_and_timeline(wrong_audit_actor, expected):
    case = {"id": "requires", "do": {"execute_step": 2, "actor": "sam"}, "expect": {"action_run": {"state": "pending_approval"}}}
    approve = {"id": "approve", "after": "requires", "do": CASES["approval-ok"]["do"], "expect": CASES["approval-ok"]["expect"]}
    def service(req):
        p = req.url.path
        if p == "/v1/debug/reset":
            return httpx.Response(204)
        if p == "/v1/alerts":
            return httpx.Response(200, json={"items": [{"id": 91}]})
        if p == "/v1/alerts/91/actions":
            assert req.headers["x-user"] == "sam" and json.loads(req.content) == {"step_index": 2}
            return httpx.Response(200, json={"id": 203, "state": "pending_approval"})
        if p == "/v1/action-runs/203/approve":
            assert req.headers["x-user"] == "lee"
            return httpx.Response(200, json={"id": 203, "state": "succeeded"})
        assert p == "/v1/alerts/91"
        actor = "rita" if wrong_audit_actor else "lee"
        return httpx.Response(200, json={"timeline": [{"kind":"audit", "text": f"{actor} {action}"}
                              for action in ("action.approve","action.execute")]})
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        runner = golden.Runner(client, api="http://api", telecom="http://phone", mailpit="http://mail", timeout=0)
        results = runner.run({"clock_default": SUITE["clock_default"], "cases":[case, approve]})
    assert [r.status for r in results] == ["PASS", expected]


@pytest.mark.parametrize("tamper,expected", [(None, "PASS"), ("line", "FAIL"), ("group", "FAIL"), ("channels", "FAIL")])
def test_log_checks_user_visible_groups_exclusions_and_digest_receipts(tmp_path, tamper, expected):
    log = tmp_path / "watched.log"
    log.touch()
    fixture_lines = (ROOT / CASES["pricing-digest"]["send"]["file"]).read_text().splitlines()
    instances = [line.split()[2] for line in fixture_lines]
    alerts = [{"id": i+1, "rule_id": "pricing-warn-logs", "group_values": {"instrument": instrument},
               "runbook_id": "pricing-latency", "occurrence_count": count}
              for i, (instrument, count) in enumerate([("EURUSD", 3), ("USDJPY", 1)])]
    if tamper == "group":
        alerts.append({**alerts[0], "id": 3, "group_values": {"instrument": "GBPUSD"}})

    def service(req):
        p = req.url.path
        if p == "/v1/debug/reset":
            return httpx.Response(204)
        if p == "/v1/events":
            return httpx.Response(200, json={"items": [{"id": i+1} for i, _ in enumerate(log.read_text().splitlines())]})
        if p.startswith("/v1/events/"):
            i = int(p.split("/")[3])
            matched = instances[i-1] == "risk-engine-1" and tamper == "line"
            return httpx.Response(200, json={"id": i, "status": "processed", "fields":{"instance":instances[i-1]},
                            "outcome":"EXISTING_ALERT" if matched else "NO_MATCH", "decisions":[]})
        if p == "/v1/alerts":
            return httpx.Response(200, json={"items": alerts})
        if p == "/api/v1/messages":
            return httpx.Response(200, json={"messages": [], "total":0})
        if p == "/messages":
            return httpx.Response(200, json={"items": []})
        assert p == "/v1/notifications"
        return httpx.Response(200, json={"items":[{"id":"u", "recipient":"team:pricing",
                                     "channel":"sms" if tamper == "channels" else "ui", "state":"delivered"}]})

    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        assert result_for(client, CASES["pricing-digest"], log_input=log).status == expected
    assert log.read_text().splitlines() == fixture_lines
