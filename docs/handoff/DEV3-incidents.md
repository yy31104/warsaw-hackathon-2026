# DEV3 — Incidents & priorities (grouping, rules, criticality, resolution)

**Branch:** `feat/processing` · **Layers:** L3 dedup/grouping, L4 decision, incident lifecycle + runbook actions
**You own:** `backend/alerting/rules/`, `runbooks/actions/`, `runbooks/approvals.py`, `api/routes_alerts.py`, `api/routes_runbooks.py`, `tests/processing/`, `db/migrations/2xx_*.sql`
**You call:** `routing.plan` and `runbooks.render` (DEV4, pure). Until they land, use a stub that returns one `ui` intent to the owner team.
**Read first:** BRIEF §1–3, CONTRACT §2, §3.3–3.5, §5, §6, §9 (your rows)

## Mission
Turn accepted events into the right incidents with exact counts (500 real failures = 1 incident × 500), with a P0–P3 priority and a recorded reason for every decision. Then manage the incident to resolution: ack, resolve, runbook actions with human approval, audit, auto-close. The deciding logic is **pure** so DEV2's simulator can replay it.

## Tasks (★ = needed for G1)

### By G1 (Sat 00:00)
1. ★ **DSL** `rules/dsl.py`: `compile(match) -> Predicate`, where `Predicate(fields) -> (bool, trace)`.
   - Ops: CONTRACT §5. Numeric coercion. Severity order applies to `$severity`. fnmatch `glob`. Capped `regex`.
   - A missing field makes `exists` false and every other op false.
   - Also accepts the flat `{path: value}` form (Appendix B `matches`).
   - **Push this first.** DEV2 needs it for schema detection and rule validation.
2. ★ **Engine (pure)** `rules/engine.py`: `evaluate(event, ruleset, state, clock) -> Evaluation`.
   - Evaluate all enabled rules; matches are not exclusive.
   - Alert key: `group.by` → schema `alert_key` → fallback.
   - Each match gives an op with a transition: `created`, `occurrence`, `severity_increased` or `cooldown_elapsed`.
   - Render the rule's `present` block (`templates.render`). Apply `resolve_when`. Non-matches give `NO_MATCH`.
   - `state.get_open(rule_id, key)` is an interface: a DB view in the worker, a dict in the simulator.
   - Tests: every positive and negative case of the four rules in the golden file.
3. ★ **Rule-worker** `python -m alerting.rules.worker --lane normal|critical --batch 200`. One transaction per batch (CONTRACT §6):
   - claim with `SKIP LOCKED` → evaluate → **pre-aggregate per (rule, key)** → upsert incidents (`ON CONFLICT (rule_id, alert_key) WHERE status <> 'resolved'`);
   - `routing.plan()` → insert notifications `ON CONFLICT (dedup_key) DO NOTHING`, plus decisions and `alert_occurrences` (keep the last 50);
   - mark events processed (`trace.processed_at`), then `NOTIFY alerts`.
   - Ruleset comes from `config/files.py` until DEV2's loader lands, then reload on `NOTIFY config`.
4. ★ **Incidents API:**
   - `GET /v1/alerts?team=&status=&q=`, sorted by status, priority/score, last_seen.
   - `GET /v1/alerts/{id}`: fields, group values, occurrences, priority + score breakdown, timeline of decisions and audit.
   - `POST /v1/alerts/{id}/ack` and `POST /v1/alerts/{id}/resolve` (`X-User` header, audit row, `NOTIFY alerts`).

### By G2 (Sat 04:00)
5. **Priority P0–P3 + score:**
   - Rule `priority` override, else derived: critical → P0, error → P1, warning → P2, info → P3.
   - Score = severity (50/35/20/5) + prod 25 + rule `impact` 0–25 + min(15, 5·log10 count), stored with a breakdown.
   - Score and priority sort the feed and pick the lane. **They never change who gets notified.**
6. **Scheduler** `python -m alerting.rules.scheduler` (every 2 s):
   - Digest incidents that are due → `digest_due` → plan → notifications (Pricing: one per instrument every 5 min).
   - `cooldown_elapsed` re-notify for incidents still receiving occurrences.
7. **Explain** `GET /v1/events/{id}/explain`: schema detection, each rule's trace, the incident op, planned notifications and the final outcome ("why was / wasn't I notified").
8. **Runbook actions** `runbooks/actions/`: one file per action + `REGISTRY`.
   - Actions: `http_get` (allow-list `counterparty-service`, `mock-ops`), `retry_workflow`, `restart_service`, `call_webhook` (allow-list), `open_ticket`, and `escalate` (an `escalation` notification to the target team's on-call via `plan()`).
   - An unknown action becomes manual and is never executed.
9. **Execute + approve + audit** in `routes_runbooks.py`:
   - `GET /v1/alerts/{id}/runbook` returns DEV4's `render()` output plus the run state of each step.
   - `POST /v1/alerts/{id}/actions {step_index}`: `approval: none` runs now; `required` moves to `pending_approval` with a 4-digit code (10 min) and an `approval_request` notification to the approvers.
   - `POST /v1/action-runs/{id}/approve|reject` returns 403 unless the actor is listed in `approvers`. Audit `action.execute` / `action.approve`, then `NOTIFY actions`.
   - **Auto-close:** an action with `resolves_alert`, a passing health check, or a schema `resolve_when` event resolves the incident (`resolved_by = system`).
   - After 2 failed `retry_workflow` runs, mark the escalate step as "suggested".

### By G3 (Sat 07:30)
10. **Critical lane:** run a dedicated `--lane critical` worker. Storm test: 500 distinct events on 2 workers → 1 incident, count 500, sam paged once.

## Acceptance
Golden cases `settlements-*`, `rates-*`, `anna-*` (matching side), `pricing-digest`, `storm-500-one-alert`, `alertmanager-auto-resolve`, `rates-missing-template-field`, `approval-*`.

## Pitfalls
- Don't group Settlements by `workflowId`; that turns 500 failures into 500 incidents. Use `[workflowName, step, error.code]`.
- No HTTP and no LLM calls inside a DB transaction.
- Use `clock_for_event(event.trace)`, never `datetime.now()`.

## Prompt for your coding agent
> Read `AGENTS.md`, `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DEV3-incidents.md` fully. You are DEV3 (incidents and priorities) on branch `feat/processing`. Work only inside your own directories. Never edit shared files; if one needs a change, stop and tell me. Do tasks 1–2 (`rules/dsl.py`, then the pure `rules/engine.py`) test-first against `config/rules.yaml` and `fixtures/golden/expectations.yaml`, then the rule-worker and the incidents API. Commit after each task, run `make test`, and push to `feat/processing`. Python 3.12, psycopg 3 async. Report what you ran and what you didn't.
