# DEV1 · Yi — Tech lead + backend core (rules, incidents, routing, delivery, runbook actions)

**Branch:** `feat/backend-yi` · **GitHub:** `yy31104`
**Layers:** L3 grouping/counting, L4 decision, L5 routing + reliable delivery, L6 runbook actions/approval
**You own:**
- Code: `backend/alerting/rules/`, `routing/`, `delivery/`, `runbooks/` (match, render, actions, approvals), `api/routes_alerts.py`, `routes_notifications.py`, `routes_runbooks.py`
- Tests: `backend/tests/processing/`, `backend/tests/delivery/`
- Migrations: `db/migrations/1xx_*.sql`
- As tech lead, the shared files: `docs/CONTRACT.md`, `AGENTS.md`, `contracts.py`, `llm.py`, `templates.py`, `clock.py`, `db.py`, `api/app.py`, `001_init.sql`, `.github/`
**Others call your pure functions:** `rules.dsl.compile`, `rules.engine.evaluate`, `routing.plan`, `runbooks.render` (daizhongtian uses them for schema detection, rule validation, the simulator and runbook structuring). Keep their signatures stable.
**Don't read:** `../private-holdout/` (Marlon runs it at G3; you write the engine it tests).

## Tech-lead duties (≈ 15 min/hour)
- G0: Marlon (`MarlonMejia15`) must accept the GitHub invite. Send each person their handoff. Agree on the NOTIFY payload shapes with daizhongtian (5 min). Ask a mentor about the deadline, the Lockbox path and the meaning of 2.4.
- Merge PRs as they come in (CI green, a quick read, the PR stays in the owner's folders). Run the G1 slice yourself from `main` at ~23:30. Record each gate in `docs/GATES.md`.
- Shared-file changes: a tiny PR, announced in the team chat first.

## Tasks (★ = needed for G1)

### By G1 (Sat 00:00)
1. ★ **DSL** `rules/dsl.py`: `compile(match) -> Predicate`, where `Predicate(fields) -> (bool, trace)`.
   - Ops from CONTRACT §5; numbers coerced; `$severity` uses severity order; fnmatch `glob`; capped `regex`.
   - A missing field makes `exists` false and every other op false.
   - Also accepts the flat `{path: value}` map (the Appendix B runbook `matches`).
   - **Push it first: daizhongtian needs it.**
2. ★ **Engine (pure)** `rules/engine.py`: `evaluate(event, ruleset, state, clock) -> Evaluation`.
   - Evaluates all enabled rules (matches are not exclusive).
   - Alert key: `group.by` → schema `alert_key` → fallback.
   - Each match produces an op with a transition: `created`, `occurrence`, `severity_increased` or `cooldown_elapsed`.
   - Renders `present` with `templates.render`; handles `resolve_when`; non-matches give `NO_MATCH`.
   - `state.get_open(rule_id, key)` is an interface (DB view in the worker, dict in the simulator).
   - Tests: all positive and negative cases for the 4 rules in `fixtures/golden/expectations.yaml`.
3. ★ **routing.plan (pure)** `routing/plan.py` + `routing/directory.py`, signature in CONTRACT §6.
   - Recipients: user / team / on-call.
   - Channels: explicit, or from preferences (first matching row; `when` in the **user's timezone** with `clock`, `[start, end)`), then team/default fallback. A critical alert always gets ≥ 1 channel.
   - Render per channel (CONTRACT §8; `short` ≤ 160 chars + `http://localhost:5173/a/<id>`).
   - Dedup key: `f"{alert.id}:{kind}:{recipient}:{channel}:{notify_epoch}"`; also returns `runbook_id`.
   - Tests: Anna 10:00 → SMS, 21:00 → email, 08:00 → SMS, 20:00 → email; Settlements → sam phone + SMS + `team:settlements` UI; Rates → rita email + UI.
4. ★ **Rule-worker** `python -m alerting.rules.worker --lane normal|critical --batch 200`, one transaction per batch (CONTRACT §6):
   - Claim with `SKIP LOCKED` → evaluate → **pre-aggregate per (rule, key)** → upsert incidents.
   - `plan()` → notifications `ON CONFLICT (dedup_key) DO NOTHING` → decisions + `alert_occurrences`.
   - Mark events processed → `NOTIFY alerts`.
   - Read the ruleset from `config/files.py` until daizhongtian's loader lands, then reload on `NOTIFY config`.
5. ★ **Providers + delivery worker**
   - Providers in `delivery/providers/`:
     - email: SMTP to Mailpit, headers `X-Notification-Id`, `X-Gen-Sent-At`;
     - sms/phone: httpx to daizhongtian's fake-telecom. 3 s timeout → `unknown`; 5xx or connection refused → `retryable`; 4xx → `permanent`;
     - ui: delivered on insert.
     - A real provider must be one new file.
   - `python -m alerting.delivery.worker`:
     - claim with `SKIP LOCKED`, set `sending` + `locked_until`, commit;
     - **send outside the transaction**, then record the attempt;
     - backoff `min(60, 2^n) ± 20 %` and `retry_after`; after 8 attempts or 15 minutes → `failed`;
     - per-channel breaker; `NOTIFY notifications`;
     - an expired lease is resent with the **same** `Idempotency-Key`.
6. ★ **Incidents API**
   - `GET /v1/alerts?team=&status=&q=` (sorted by status, priority/score, last_seen).
   - `GET /v1/alerts/{id}` (fields, group values, occurrences, priority + score breakdown, timeline).
   - `POST /ack` and `POST /resolve` (`X-User`, audit, `NOTIFY alerts`).
   - `GET /v1/alerts/{id}/notifications` (+ attempts) and `GET /v1/notifications?state=`.

### By G2 (Sat 04:00)
7. **Priority P0–P3 + score.**
   - Priority: the rule's `priority`, otherwise critical → P0, error → P1, warning → P2, info → P3.
   - Score: severity 50/35/20/5 + prod 25 + `impact` 0–25 + min(15, 5·log10 count), stored with its breakdown.
   - Both only sort the feed and choose the lane. **They never change routing.**
8. **Scheduler** `python -m alerting.rules.scheduler` (every 2 s):
   - due digests → `digest_due` → plan → notifications (Pricing: per instrument, every 5 min);
   - `cooldown_elapsed` re-notify.
9. **Explain** `GET /v1/events/{id}/explain`: schema detection, each rule's trace, the incident op, planned notifications, outcome.
10. **Runbooks.**
    - Match + render (pure) in `runbooks/match.py` and `render.py`:
      - yaml-steps → check / action / if;
      - Markdown numbered or checkbox lines → manual steps, where "approval" wording sets `approval: required`;
      - text → one manual step + `can_structure: true`;
      - url → a link.
    - Values are filled in via `templates.render`. Exposed at `GET /v1/alerts/{id}/runbook` (steps + run state).
11. **Actions + approval + audit.**
    - `runbooks/actions/`: one file per action + `REGISTRY`.
      - `http_get` (allow-list: `counterparty-service`, `mock-ops`), `retry_workflow`, `restart_service`, `call_webhook` (allow-list), `open_ticket`.
      - `escalate`: escalation notification via `plan()`.
      - An unknown action falls back to manual and is never executed.
    - `POST /v1/alerts/{id}/actions {step_index}`:
      - `approval: none` → runs immediately;
      - `required` → `pending_approval`, a 4-digit code valid 10 min, and an `approval_request` SMS to the approvers.
    - `POST /v1/action-runs/{id}/approve|reject` returns 403 unless the actor is an approver. Every execute/approve goes to the audit log, followed by `NOTIFY actions`.
    - Auto-close via `resolves_alert`, a passing health check, or `resolve_when`.
    - After 2 failed retries, mark the escalate step "suggested".

### By G3 (Sat 07:30), P1 in order
12. **Reply-to-approve** `POST /v1/inbound/{channel} {from, body}`: `YES|NO <code>` → the sender's contact must belong to an approver → check expiry and single use.
13. **Critical lane:** a dedicated `--lane critical` worker; delivery claims `ORDER BY priority`. Storm test: 500 distinct events on 2 workers → 1 incident, count 500, sam paged once.
14. **Channel fallback:** a critical notification whose breaker stays open > 30 s → `fallback` on the next preferred channel. Also throttle per recipient (`recent_sends` → `throttle_summary`).

## Acceptance (daizhongtian's golden runner; Marlon re-checks at G3)
- Golden cases: `settlements-*`, `rates-*`, `anna-*`, `pricing-digest`, `storm-500-one-alert`, `alertmanager-auto-resolve`, `rates-missing-template-field`, `settlements-runbook-rendered`, `approval-*`, `channel-down-no-loss`.
- `kill -9` a delivery worker mid-send → the message is resent with the same key, and fake-telecom shows 1 unique message.

## Pitfalls
- Don't group Settlements by `workflowId`; use `[workflowName, step, error.code]`.
- No HTTP or LLM calls inside a DB transaction.
- A timeout is `unknown`, not failed.
- Anna's time window is in her timezone: use `clock_for_event(event.trace)`.

## Prompt for your coding agent
> Read `AGENTS.md`, `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DEV1-backend-Yi.md` fully. You are DEV1 (backend core) on branch `feat/backend-yi`. Work only inside the directories under "You own" (code part). Do tasks 1–3 (`rules/dsl.py`, pure `rules/engine.py`, pure `routing/plan.py`) test-first against `config/*.yaml` and `fixtures/golden/expectations.yaml`. Then build the rule-worker, the providers + delivery worker, and the incidents API. Commit after each task, run `make test`, and push to `feat/backend-yi`. Python 3.12, psycopg 3 async, httpx. Report what you ran and what you didn't.
