# Handoff — BACKEND (rules, alerts, routing, delivery, runbook actions) · owner: Yi

**Branch:** `backend/core` · **Layers:** L3 dedup/grouping, L4 decision, L5 routing + reliable delivery, L6 actions/approval
**You own:** `backend/alerting/rules/`, `routing/`, `delivery/`, `runbooks/`, `api/routes_alerts.py`, `routes_notifications.py`, `routes_runbooks.py`, `db/migrations/1xx_*.sql`
**You do NOT own (DATA does):** config CRUD APIs, every LLM feature, the simulator harness, the mocks, SSE, the load test. You expose **pure functions** that DATA calls (`dsl.compile`, `engine.evaluate`, `routing.plan`, `runbooks.render`).
**Read first:** BRIEF §1–2, CONTRACT §2, §3.3–3.5, §5, §6, §8, §9 (your rows)

## Mission
Turn accepted events into the right alerts with exact counts (500 real failures = 1 alert × 500). Get each alert to the right people on the right channel, and never lose it. Run runbook steps safely with approval and audit. Everything stateful is in Postgres. The deciding logic is pure, so the simulator can replay it.

## Tasks (in order; ★ = on the G1 critical path)

### By G1 (Sat 00:00)
1. ★ **DSL** `rules/dsl.py`: `compile(match) -> Predicate`, where `Predicate(fields) -> (bool, trace)`.
   - Ops: CONTRACT §5. Numeric coercion; severity order for `$severity`; fnmatch `glob`; capped `regex`.
   - Missing field → `exists` false, everything else false.
   - Accepts the flat `{path: value}` map (Appendix B runbook `matches`).
   - **Push it to `main` first.** DATA uses it for schema detection and rule validation.
2. ★ **Engine (pure)** `rules/engine.py`: `evaluate(event, ruleset, state, clock) -> Evaluation`.
   - All enabled rules (matches are not exclusive) → alert key (`group.by` → schema `alert_key` → fallback) → op with transition (`created / occurrence / severity_increased / cooldown_elapsed`).
   - Rendered `present` (`templates.render`). `resolve_when`. `NO_MATCH`.
   - `state.get_open(rule_id, key)` is an interface: a DB view in the worker, a dict in DATA's simulator.
   - Tests: every positive and negative case for the 4 rules in `fixtures/golden/expectations.yaml`.
3. ★ **routing.plan (pure)** `routing/plan.py` + `routing/directory.py`, signature in CONTRACT §6:
   - **Recipients:** user / team / oncall.
   - **Channels:** explicit, or per user preferences (first matching row; `when` in the **user's timezone**, `[start, end)`, `clock`), then team/default fallback. **A critical alert always gets ≥ 1 channel.**
   - **Render per channel** (CONTRACT §8; `short` ≤ 160 chars + link `http://localhost:5173/a/<id>`).
   - **dedup_key:** `f"{alert.id}:{kind}:{recipient}:{channel}:{notify_epoch}"`.
   - **Runbook:** `runbook_id` (task 7).
   - Tests: Anna 10:00 → SMS, 21:00 → email, 08:00 → SMS, 20:00 → email; Settlements → sam phone + sms + `team:settlements` ui; Rates → rita email + ui.
4. ★ **Rule-worker** `python -m alerting.rules.worker --lane normal|critical --batch 200`, one transaction per batch (CONTRACT §6 order):
   - Claim with `SKIP LOCKED` → evaluate → **pre-aggregate per (rule, key)** → upsert alerts (`ON CONFLICT (rule_id, alert_key) WHERE status <> 'resolved'`).
   - `plan()` → insert notifications (`ON CONFLICT (dedup_key) DO NOTHING`) + decisions + `alert_occurrences` (keep the last 50).
   - Mark events processed with `trace.processed_at`, then `NOTIFY alerts`.
   - Ruleset from `config/files.py`, switching to DATA's loader (+ reload on `NOTIFY config`) when it lands.
5. ★ **Providers + delivery worker:**
   - Providers `delivery/providers/{email,telecom,ui}.py`:
     - email: SMTP → Mailpit, headers `X-Notification-Id`, `X-Gen-Sent-At`.
     - sms/phone: httpx → fake-telecom `/sms` `/call`. 3 s timeout → `unknown`; 5xx / connection refused → `retryable`; 4xx → `permanent`.
     - ui: delivered on insert.
     - Adding Twilio must be one new file.
   - `python -m alerting.delivery.worker`:
     - Claim due rows with `SKIP LOCKED`, set `sending` + `locked_until = now() + 30s`, commit.
     - **Send outside the transaction.** Record the attempt and the state.
     - Backoff `min(60, 2^n) ± 20 %`, honour `retry_after`. Max 8 attempts / 15 min → `failed`.
     - Per-channel breaker (5 failures → open 10 s). Then `NOTIFY notifications`.
   - Expired leases are re-claimable and reuse the **same** `Idempotency-Key`.
6. ★ **Alerts API:** `GET /v1/alerts?team=&status=&q=` (sorted by status, score, last_seen), `GET /v1/alerts/{id}` (fields, group values, occurrences, score breakdown, timeline), `POST /ack`, `POST /resolve` (`X-User`, audit, `NOTIFY alerts`), `GET /v1/alerts/{id}/notifications` (+ attempts), `GET /v1/notifications?state=`.

### By G2 (Sat 04:00)
7. **Runbooks** `runbooks/match.py` + `runbooks/render.py`:
   - Match by `rules[]`, `matches` or the yaml-steps' own `matches` → `plan.runbook_id`.
   - `render(runbook, fields) -> steps` (pure; DATA's structuring output uses the same step shape):
     - **yaml-steps** → check / action / if;
     - **markdown** numbered or checkbox lines → manual steps, with "approval" wording → `approval: required`;
     - **text** → one manual step + `can_structure: true`;
     - **url** → link.
   - `GET /v1/alerts/{id}/runbook`.
8. **Actions + approval + audit** `runbooks/actions/` (one file per action, `REGISTRY`): `http_get` (allow-list: `counterparty-service`, `mock-ops`), `retry_workflow`, `restart_service`, `call_webhook` (allow-list), `open_ticket`, `escalate` (`escalation` notification via `plan()` to the target team's on-call). Unknown action → manual, never executed.
   - `POST /v1/alerts/{id}/actions {step_index}`: `approval: none` runs now; `required` → `pending_approval` + 4-digit code (10 min) + `approval_request` SMS to approvers.
   - `POST /v1/action-runs/{id}/approve|reject` (403 unless the actor is in `approvers`). Audit `action.execute` / `action.approve`. `NOTIFY actions`.
   - **Auto-close:** an action with `resolves_alert`, a passing health check, or a schema `resolve_when` → resolved by `system`. After 2 failed `retry_workflow` runs, mark the escalate step "suggested".
9. **Scheduler** `python -m alerting.rules.scheduler` (every 2 s): digest alerts due → `digest_due` → plan → notifications (Pricing: per instrument every 5 min); `cooldown_elapsed` re-notify.
10. **Explain** `GET /v1/events/{id}/explain`: schema detection (from `events`) + each rule's trace + alert op + planned notifications + outcome.

### By G3 (Sat 07:30), P1 in this order
11. **Reply-to-approve** `POST /v1/inbound/{channel} {from, body}`: `YES|NO <code>` → map the sender's contact to a user → approver + code + expiry + single-use checks.
12. **Critical lane + score:**
   - Score = severity (critical 50 / error 35 / warning 20 / info 5) + prod 25 + rule `impact` 0–25 + min(15, 5·log10 count), stored with a breakdown. **It never changes routing.**
   - Run a dedicated `--lane critical` worker. Delivery claims `ORDER BY priority`.
13. **Channel fallback:** a critical notification whose breaker stays open > 30 s → `fallback` notification on the next preferred channel (`fallback_of`); keep retrying the original.
14. **Throttle per recipient** (`throttle:` in a rule): the worker queries `recent_sends` and `plan()` emits a `throttle_summary`.

## Acceptance (golden runner; the REVIEW person re-runs it at G3)
All `settlements-*`, `rates-*`, `anna-*`, `pricing-digest`, `storm-500-one-alert` (2 workers → 1 alert, count 500, sam paged once), `alertmanager-auto-resolve`, `rates-missing-template-field`, `settlements-runbook-rendered`, `approval-*`, `channel-down-no-loss`; and `kill -9` of a delivery worker mid-send → re-sent with the same key (fake-telecom shows 1 unique).

## Pitfalls
- Don't group Settlements by `workflowId` (that gives 500 alerts). The rule uses `[workflowName, step, error.code]`.
- No HTTP and no LLM inside a DB transaction. A timeout is `unknown`, not failed.
- Anna's window is in **her** timezone. Use `clock_for_event(event.trace)`.
- Don't read `/Users/…/private-holdout/`: you build the engine it tests (BRIEF §6).

## Starter prompt for your coding agent
> You are implementing the BACKEND role of the Alert Funnel hackathon project. Read `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/BACKEND.md` fully first. Work only inside the directories listed under "You own". Do not edit shared files (`contracts.py`, `llm.py`, `db.py`, `clock.py`, `templates.py`, `api/app.py`, `001_init.sql`); if one needs a change, stop and tell me. Do tasks 1–3 (dsl, pure engine, pure routing.plan) test-first against `config/*.yaml` and `fixtures/golden/expectations.yaml`, then the rule-worker, the delivery worker and the alerts API. Commit after each task and run `make test`. Python 3.12, psycopg 3 async, httpx. Report what you ran and what you did not run.
