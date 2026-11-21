"""Seed synthetic incidents and verify the W2 unknown-outcome receiver boundary."""
import json
import os
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

API = os.environ.get("W0_API_URL", "http://api:8000")
OPS = os.environ.get("W0_MOCK_OPS_URL", "http://mock-ops:8200")
mode = os.environ.get("W0_UNKNOWN_MODE", "verify")
assert mode in {"verify", "seed", "fixture"}
tag = uuid4().hex[:8]


def call(origin, path, body=None, headers=None, timeout=9):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    try:
        with urlopen(Request(origin + path, data=data, headers=h), timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def ok(origin, path, body=None, headers=None):
    status, payload = call(origin, path, body, headers)
    assert 200 <= status < 300, (path, status, payload)
    return payload


def incident(label):
    wf = f"W0-{label}-{tag}"
    event = {"type":"WORKFLOW_FAILED", "workflowId":wf, "workflowName":"order-settlement",
             "step":f"validate-counterparty-{label.lower()}-{tag}", "severity":"ERROR","environment":"prod",
             "timestamp":datetime.now(timezone.utc).isoformat(),
             "error":{"code":"TIMEOUT","message":"Synthetic settlement demo timed out"}}
    event_id, = ok(API, "/v1/events/generic-webhook", event, {
        "X-Api-Key": "demo-generic-key", "Idempotency-Key": wf,
    })["event_ids"]
    for _ in range(90):
        explanation = ok(API, f"/v1/events/{event_id}/explain")
        for d in explanation.get("decisions", []):
            if d.get("rule_id") == "settlements-wf-failed" and d.get("alert_id"):
                return wf, d["alert_id"]
        time.sleep(0.20)
    raise RuntimeError(f"No settlement incident for {event_id}")


def arm():
    r = ok(OPS, "/demo/delay-next", {"action": "retry_workflow"})
    assert r.get("simulated") and r.get("action") == "retry_workflow", r
    return r


workflow, alert = incident("UNKNOWN")
if mode in {"verify", "seed"}:
    arm()
t = time.monotonic()
run = ok(API, f"/v1/alerts/{alert}/actions", {"step_index": 1}, {"X-User": "sam"})
run_id = run["id"]
key = f"action-{run_id}"
if mode == "fixture":
    # Keep a real one-attempt simulator receipt. Seed only this newly generated
    # synthetic client's visible state without changing the gateway's global mode.
    import psycopg
    from psycopg.types.json import Jsonb

    assert run["state"] == "succeeded", run
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        updated = conn.execute(
            """UPDATE action_runs
               SET state='unknown', result=%s, updated_at=now()
               WHERE id=%s AND alert_id=%s AND requested_by='sam' AND action='retry_workflow'
                 AND state='succeeded' AND params->>'workflowId'=%s
               RETURNING id""",
            (Jsonb({"ok": False, "error": "Synthetic demo starts with a missing client reply"}),
             run_id, alert, workflow),
        ).fetchone()
        assert updated == (run_id,), (updated, run_id)
        conn.execute(
            """INSERT INTO audit_log(actor, action, target_kind, target_id, detail)
               VALUES ('w0-demo', 'action.demo_seed', 'alert', %s, %s)""",
            (str(alert), Jsonb({"run_id": run_id, "scope": "synthetic missing-reply fixture"})),
        )
    book = ok(API, f"/v1/alerts/{alert}/runbook")
    assert any(step.get("run_id") == run_id and step.get("state") == "unknown" for step in book["steps"])
else:
    assert run["state"] == "unknown", run
    unknown_seconds = round(time.monotonic() - t, 3)
blocked, detail = call(API, f"/v1/action-runs/{run_id}/retry", {"confirm": True}, {"X-User": "sam"})
assert blocked == 409, (blocked, detail)
receipt = ok(OPS, f"/executions/{key}")
result = {"workflow":workflow,"alert":alert,"incident_path":f"/a/{alert}#runbook",
          "run_id":run_id,"action_key":key,"visible_before":"unknown","confirmed_retry_http":blocked,
          "actual_receiver_state":receipt["state"],"receiver_attempt_count":receipt["result"]["output"]["attempt"],
          "mode":mode}
if mode != "fixture":
    result["elapsed_until_unknown_seconds"] = unknown_seconds
assert result["actual_receiver_state"] == "succeeded" and result["receiver_attempt_count"] == 1
if mode == "verify":
    after = ok(API, f"/v1/action-runs/{run_id}/reconcile", {}, {"X-User": "lee"})
    assert after["id"] == run_id and after["state"] == "succeeded"
    same_receipt = ok(OPS, f"/executions/{key}")
    assert same_receipt == receipt
    result.update(reconciled_as="lee", state_after=after["state"], receipt_unchanged=True, status="PASS")
elif mode == "seed":
    ready_wf, ready_id = incident("RECORD")
    arm()
    result.update(recording_path=f"/a/{ready_id}#runbook", recording_workflow=ready_wf,
                  recording_action_armed=True, status="PASS")
else:
    result.update(synthetic_fixture=True, global_delay_armed=False, status="PASS")
print(json.dumps(result, indent=2))
