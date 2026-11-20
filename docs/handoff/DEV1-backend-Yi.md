# DEV1 · Yi — Tech lead + backend core (rules, incidents, routing, delivery, runbook actions)

**Branch:** `feat/backend-yi` · **GitHub:** `yy31104`
**Layers:** L3 grouping/counting, L4 decision, L5 routing + reliable delivery, L6 runbook actions/approval
**You own:**
- Code: `backend/alerting/rules/`, `routing/`, `delivery/`, `runbooks/` (match, render, actions, approvals), `api/routes_alerts.py`, `routes_notifications.py`, `routes_runbooks.py`
- Tests: `backend/tests/processing/`, `backend/tests/delivery/`
- Migrations: `db/migrations/1xx_*.sql`
- Moved to you (Fri 22:00): `mocks/mock_ops/`, `loadtest/golden_runner.py`
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

## Round 2 (added Fri 21:30) — hardening first, then two innovations
Status at this point: PR #1 merged, PR #2 open (hot reload, scoped approvals, pinned API shapes). Do R1–R5 before anything new; each with real-PostgreSQL tests. Semantics are in CONTRACT §4 (`transforms`, `episode_key`), §5 (processing failures), §8 (action runs), §9 (inbound secret). New golden cases: `stale-recovery-keeps-newer-episode`, `poison-event-isolated`, `inbound-approval-*`, `rule-safety-counterexample`.

- **R1 Poison-event isolation.** Classify failures:
  - **Deterministic bad input** → `quarantined` + `QUARANTINED` decision + reason.
  - **Transient** (connection lost, statement/lock timeout, serialization failure) → roll back and retry after a backoff. Never quarantine for these.
  - **A rule that raises on valid events** → `RULE_FAULT` for that rule only; skip it and keep the rest running.
  - A failing batch is bisected to find the offender. Each event gets a retry budget (`trace.attempts`, max 3).
  - Honour `trace.demo_fault == "raise"` in DEMO_MODE (test hook).
  - Migration `1xx`: add `quarantined` to the `events.status` check constraint.
  - Test: 3 events, the middle one raises → 2 processed, 1 quarantined, worker still running, counts correct.
- **R2 Approval security.**
  - Codes: 6 digits, 5-minute expiry, single use.
  - Failed attempts are persisted per sender: 5 in 15 min → 429 + audit `approval.locked`.
  - `POST /v1/inbound/{channel}` requires `X-Provider-Secret` == env `INBOUND_SECRET` (401 otherwise).
  - `X-User` stays documented as demo-only.
  - Tests: missing secret, wrong sender, wrong code ×5, expired, replay, two concurrent approvals.
- **R3 Database timeouts.**
  - Set `statement_timeout`, `lock_timeout` and `idle_in_transaction_session_timeout` on pool connections (in `db.py`, which you own as tech lead).
  - Size them from measured batch latency; make them configurable via env.
  - A timeout is classified transient → rollback + retry.
  - Test: hold a row lock on a hot alert in another connection → the worker times out, retries, and the final count is exact (no loss, no double count).
- **R4 Stale recovery.**
  - Store `latest_episode` on alerts (migration `1xx`), taken from the schema's `episode_key` (`$episode`).
  - A resolve closes the alert only if its `$episode` == `latest_episode`; otherwise `STALE_RESOLVE` and the alert stays open.
  - Add `episode_key: "{{startsAt}}"` to the alertmanager schema in `config/schemas.yaml`.
- **R5 Action timeouts.**
  - Every action call sends `Idempotency-Key: action-<run_id>`.
  - A timeout → `unknown` ("needs verification"). Migration `1xx`: add `unknown` to the `action_runs.state` check.
  - A "Check result" call queries mock-ops `GET /executions/<key>` (daizhongtian adds it) before any re-run.
  - A re-run requires an explicit operator confirmation.
- **R6 Rule Safety Checker (innovation, pure)** `rules/safety.py`: `check(rule_draft, ruleset, schemas, directory) -> {scenarios_checked, violations}`.
  - **Scenarios:** for each condition, boundary values that satisfy and violate it (eq/in values, `gt` threshold ± 1, a glob sample); severity ∈ {critical, error, warning}; the configured environments; times {07:59, 08:00, 19:59, 20:00, 23:10} in each recipient's timezone; contact present / missing.
  - **Policies (check declared requirements only; UI-only rules are valid):**
    - (a) a rule with explicit channels (e.g. Settlements `[phone, sms]`) delivers **all** of them to ≥ 1 person with a usable contact in every matching scenario; a team's optional `required_channels` (teams.yaml) is enforced for that team's rules; a `preferences` route still yields ≥ 1 channel for critical;
    - (b) every route target resolves to ≥ 1 user;
    - (c) the rule matches at least one known-schema sample (otherwise warn "never fires");
    - (d) regex too long or too complex → warn.
  - Reports what it checked; never claims "safe". daizhongtian exposes it at `POST /v1/rules/check`.
- **R7 Engine support for sample verification:** make sure `evaluate` + `plan` can run side-effect-free on a draft schema (with `transforms`) for `POST /v1/schemas/verify` (daizhongtian's route). Return the deciding fields from the match trace.
- **R8 (P1, after the above)** hysteresis `group.stable_for`; then **measure** decision write volume (decisions per 1,000 events). Compact `NO_MATCH` rows (one row per event, listing the rules checked) only if the load test shows it matters, and keep "why wasn't I notified" answerable.
- **Not now:** cross-rule SMS merging (different rules can mean different incidents, permissions and runbooks), canary, dynamic escalation, cost ladder, COPY, Redis.

**After R1–R8:** your Tier 2 items in [IMPROVEMENTS.md](../IMPROVEMENTS.md), in this order: #4 late events, #5 DST tests, #1 silences, #2 unacked escalation, #3 heartbeat rules, #6 graceful shutdown (workers), #10 masking in render, #13 guarded dynamic escalation, #12 canary bypass path.

**Prompt for round 2:**
> Read AGENTS.md, docs/BRIEF.md §4, docs/CONTRACT.md §4, §5, §8, §9 and the "Round 2" section of docs/handoff/DEV1-backend-Yi.md. Merge origin/main first. Implement R1–R5 in order on feat/backend-yi, each test-first against real PostgreSQL, one commit per item. Then R6 and R7. Don't redesign the architecture and don't add the "Not now" items. Report per item: PASS / FAIL / NOT_RUN, with the exact test or command.

## Full target list (all required, in order) — updated Fri 22:00
✅ marks behavior verified on integrated main. Everything else is a target. Details are in the sections above, in CONTRACT and in [IMPROVEMENTS.md](../IMPROVEMENTS.md).

**Done**
- ✅ Round 1 tasks 1–14 (PR #1, merged).
- ✅ Hot reload, scoped approval codes, API shapes pinned to `fixtures/api`, NOTIFY envelopes, canonical-event contract test, inbox→delivery e2e (PR #2: clean merge with main, 81 tests, CI green → merge it).

**Integration**
- ✅ When daizhongtian's `ingest/normalize.py` lands: replace the test mini-normaliser, and run e2e from a real RabbitMQ publish.
- ☐ Keep merging teammates' PRs.

**Tier 0 — hardening**
- ✅ R1 poison-event isolation
- ✅ R2 approval security (6 digits, attempt limit, `X-Provider-Secret`)
- ✅ R3 DB timeouts
- ✅ R4 stale recovery (`episode_key`, `STALE_RESOLVE`)
- ✅ R5 action timeout = `unknown` + verification

**Tier 1**
- ✅ R6 Rule Safety Checker (declared requirements only)
- ✅ R7 engine support for `/schemas/verify` (draft schema + transforms, deciding fields)
- ✅ R8 hysteresis `stable_for` + measured decision volume

**Moved to you from daizhongtian**
- ✅ mock-ops `mocks/mock_ops/`: `/health` toggle, `/workflows/{id}/retry` (fail N then succeed), `/services/{name}/restart`, `/tickets`, `/metrics/pool`, `/deployments`. Every call is deduplicated by `Idempotency-Key`, and `GET /executions/<key>` reports it. Simulated data is labelled. Compose alias `counterparty-service`.
- ☐ Golden runner `loadtest/golden_runner.py`: black box over the public API, fake-telecom `/messages` and the Mailpit API; reset between cases; writes the PASS/FAIL table into GATES.md. Covers all cases in `fixtures/golden/expectations.yaml`.
  - **You build the runner; you don't own the answers.** Expected results come from the task text (the golden file) and Marlon's private holdout. If a case fails, fix the code or mark the case `DISPUTED` with a reason for Marlon to decide. Never edit an expected value to make a test pass.

**Tier 2**
- ✅ #4 late events never reopen an old episode
- ✅ #5 DST tests
- ✅ #1 silences (`SILENCED`)
- ✅ #2 unacked escalation
- ✅ #3 heartbeat / deadman rules
- ✅ #6 graceful shutdown (workers)
- ✅ #10 masking of `sensitive` fields in render
- ✅ #13 guarded dynamic escalation
- ✅ #12 canary: sender + bypass alarm
- ✅ #11 correlation IDs in your logs

**Tier 3**
- ✅ #15 cross-rule merging (opt-in)
- ✅ #16 cost ladder (opt-in, never overrides explicit channels)
- ✅ #18 shadow-rule engine state (separate counts and cooldowns, never sends)
- ✅ #19 SLO scoring from request totals
- ✅ #20 incident memory (store + lookup of human-confirmed resolutions)
- ✅ #22 fair claiming across sources/teams

**Measure first:** act on daizhongtian's load-test evidence (e.g. compact `NO_MATCH` only if decisions dominate).

### Verification on integrated main

All 394 backend tests passed on `b6e14a9` with live PostgreSQL, RabbitMQ and the loopback fake-telecom gateway. Run `make test` with the environment in `.github/workflows/ci.yml` to include the broker and gateway tests. The commands below reproduce individual checks from `backend/` with those same services. The checkout contains the bundled canary configuration.

| DEV1 target | Result | Focused verification |
| --- | --- | --- |
| Canonical ingestion, broker delivery and published-schema reload | PASS | `uv run pytest -q tests/processing/test_normalizer_contract.py tests/processing/test_broker_e2e.py tests/processing/test_e2e_schema_review.py` |
| R1, R2, R3, R4, R5 | PASS | `uv run pytest -q tests/processing/test_isolation.py tests/processing/test_approval_codes.py tests/processing/test_db_timeouts.py tests/processing/test_episodes.py tests/processing/test_action_verification.py` |
| R6, R7 | PASS | `uv run pytest -q tests/processing/test_safety.py tests/processing/test_safety_verification.py`. R7 covers the draft-verification engine. Its public HTTP route belongs to DEV2. |
| R8 | PASS | `uv run pytest -q -s tests/processing/test_stable_for.py tests/processing/test_e2e_decision_volume.py`. On 1,000 distinct workflow failures sharing one alert key, all 1,000 were processed. PostgreSQL stored 4,000 decisions (1 new alert, 999 cooldown suppressions and 3,000 nonmatches). This measures decision volume for this workload. |
| Simulated mock-ops receiver and Compose alias | PASS | `uv run pytest -q tests/tools/test_mock_ops.py`. From the repo root, `docker compose --env-file .env.example -f docker-compose.yml -f mocks/mock_ops/compose.yaml config --quiet` validated the overlay. DEV2 received the base Compose integration in [#13](https://github.com/yy31104/warsaw-hackathon-2026/pull/13#issuecomment-7051936512). |
| Golden runner tooling | PASS | `uv run pytest -q tests/tools/test_golden_runner.py tests/tools/test_golden_cases.py` |
| Golden run against the public stack | NOT_RUN | All 32 generated case rows in [GATES](../GATES.md#golden-run) await `POST /v1/debug/reset` on main. After it lands, regenerate the section with the runner against an isolated stack. |
| #4, #5, #1 | PASS | `uv run pytest -q tests/processing/test_worker_episode_integration.py tests/processing/test_dst_routing.py tests/processing/test_worker_silence_integration.py` |
| #2, #3, #6 workers | PASS | `uv run pytest -q tests/processing/test_scheduler_escalation.py tests/processing/test_heartbeat.py tests/processing/test_shutdown_rule_worker.py tests/processing/test_shutdown_delivery.py tests/processing/test_shutdown_scheduler.py` |
| #10, #13, #12, #11 | PASS | `uv run pytest -q tests/processing/test_worker_masking.py tests/processing/test_worker_scoring_integration.py tests/processing/test_e2e_canary_config.py tests/tools/test_canary_live.py tests/processing/test_e2e_correlation.py` |
| #15, #16, #18 | PASS | `uv run pytest -q tests/processing/test_merge_routing.py tests/processing/test_ladder_scheduler.py tests/processing/test_worker_shadow_integration.py` |
| #19, #20, #22 | PASS | `uv run pytest -q tests/processing/test_scoring.py tests/processing/test_worker_scoring_integration.py tests/tools/test_memory_api.py tests/processing/test_fair_sources.py tests/processing/test_fair_delivery.py` |

W0 merged the assigned windows in the agreed order, W3 [#11](https://github.com/yy31104/warsaw-hackathon-2026/pull/11), W1 [#10](https://github.com/yy31104/warsaw-hackathon-2026/pull/10), W2 [#12](https://github.com/yy31104/warsaw-hackathon-2026/pull/12). The broader teammate integration item remains ongoing.
