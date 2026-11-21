"""Install synthetic Stability or Optimizer examples in the named demo stack."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
import yaml
from psycopg.types.json import Jsonb

API = os.environ.get("W0_API_URL", "http://api:8000")
kind = os.environ["W0_INNOVATION_SEED"]

def request(path, body=None, actor=None, headers=None, accepted=(200,)):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type":"application/json", **(headers or {})}
    if actor:
        h["X-User"] = actor
    try:
        with urlopen(Request(API + path, data=data, headers=h), timeout=30) as reply:
            status, result = reply.status, json.load(reply)
    except HTTPError as exc:
        status, result = exc.code, json.load(exc)
    assert status in accepted, (path, status, result)
    return status, result

def seed_stability():
    tag = uuid4().hex[:8]
    schema_id = f"stability-workbench-{tag}"
    service = f"risk-threshold-rehearsal-{tag}"
    seed = {"kind": "stability-mock-reading-" + tag, f"review_case_{tag}": True,
            "signal": {"service": service, "severity": "P1", "message": "Synthetic ambiguous grade"}}
    rule = {"id":schema_id, "owner":"team:settlements", "description":"Compare a producer severity before publishing its mapping",
            "match":{"all":[{"field":"$service","op":"eq","value":service},
                            {"field":"$severity","op":"eq","value":"critical"}]},
            "route":[{"to":"user:sam","channels":["sms"]}],
            "group":{"by":["$service"],"mode":"immediate","cooldown":"5m"},
            "present":{"title":"Ambiguous monitoring severity"}}
    request("/v1/rules", rule, "lee", accepted=(201,))
    _, ingested = request("/v1/events/generic-webhook", seed,
                          headers={"X-Api-Key": "demo-generic-key", "Idempotency-Key": schema_id}, accepted=(202,))
    event, = ingested["event_ids"]
    for _ in range(60):
        _, issues = request("/v1/schema-issues")
        issue = next((row for row in issues["items"] if row["sample_event_id"] == event),None)
        if issue:
            break
        time.sleep(.2)
    assert issue, ("sample must stay in review", event)
    draft = {"id":schema_id, "name":"demo.stability." + tag, "detect":{"kind":seed["kind"]},
             "aliases":{"service":"signal.service","severity":"signal.severity","message":"signal.message"}}
    _, report = request("/v1/schemas/stability", {"draft":draft,"samples":[{"event":seed,"source":"generic-webhook"}],
                        "hypotheses":[{"id":"grade","kind":"severity","path":"signal.severity","label":"P1",
                                       "candidates":[{"value":"warning"},{"value":"critical"}]}]}, actor="ops")
    _, stored = request(f"/v1/events/{event}")
    assert stored["status"]=="needs_review", stored["status"]
    assert report["complete"] and report["status"]=="VARIANT" and report["critical_possible"], report
    return {"status":"PASS","event_id":event,"rule_id":schema_id,
            "path":"/sources?fingerprint="+quote(issue["fingerprint"], safe=""),
            "actor":"ops","capture":report, "event_status":stored["status"]}

def seed_optimizer():
    fixture = yaml.safe_load(Path("/app/fixtures/protected/optimizer_demo.yaml").read_text())
    rule, history = fixture["rule"], fixture["history"]
    code, response = request("/v1/rules",rule,"rita",accepted=(201,409))
    if code == 409:
        _, rules = request("/v1/rules")
        live_rule = next(row for row in rules["items"] if row["id"]==rule["id"])
        for key in ("owner","match","route","group"):
            assert live_rule[key]==rule[key], (key, live_rule[key],rule[key])
    start = datetime.fromisoformat(history["first_at"])
    prepared = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for i in range(history["count"]):
            at = start + timedelta(seconds=i * history["every_s"])
            key = f"w0-optimizer-v1-{i:02d}"
            result = conn.execute("""INSERT INTO events
                (source_id,transport,source_event_id,payload_hash,raw,fields,severity,trace,status)
                VALUES ('optimizer-fixture','http',%s,%s,'{}',%s,'warning',%s,'processed')
                ON CONFLICT (source_id, source_event_id) WHERE source_event_id IS NOT NULL DO UPDATE
                SET received_at=now() RETURNING id""",
                (key,key,Jsonb(history["fields"]),Jsonb({"demo_clock":at.isoformat()}))).fetchone()
            prepared.append(result[0])
    _, report = request(f"/v1/rules/{rule['id']}/optimize/preview",{"window":"24h"},"rita")
    best = report["options"][0]
    rejected = report["best_rejected"]
    assert report["baseline"]["night_interruptions"]==18, report["baseline"]
    assert best["savings"]["night_interruptions"]==14 and best["protected_violations"]==0, best
    assert (rejected["counterexample"]["channel"], rejected["counterexample"]["kind"])==("sms","missing"),rejected
    return {"status":"PASS","rule_id":rule["id"],"path":"/rules","actor":"rita","seed_count":len(prepared),
            "baseline":report["baseline"],"after":best["metrics"],"option":best["id"],"savings":best["savings"],
            "protected_cases":best["protected_cases"],"protected_violations":best["protected_violations"],
            "rejected":rejected["counterexample"],"events_replayed":report["events_replayed"],
            "candidates_evaluated":report["evaluated_count"]}

print(json.dumps({"stability":seed_stability,"optimizer":seed_optimizer}[kind](),indent=2,default=str))
