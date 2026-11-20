# Handoff — RULES (Backend: rules & alert engine, the "algorithm" role)

**Branch:** `rules/engine` · **Layers:** L3 dedup/grouping, L4 decision, rule simulator
**You own:** `backend/alerting/rules/`, `assist/rule_draft.py`, `assist/fix_proposal.py`, `api/routes_rules.py`, `api/routes_alerts.py`, `db/migrations/1xx_*.sql`
**Read first:** BRIEF §1–2, CONTRACT §2, §3.3–3.5, §5, §6, §9 (your rows), §10

## Mission
Turn accepted events into the right alerts, with exact counts: 500 real failures = 1 alert × 500. Each team's rules, written on any field, are evaluated by **one pure engine**. The rule-worker uses it live, and the simulator replays history through the same engine without side effects.

## Tasks (in order)

### By G1 (Sat 00:00)
1. **DSL compiler** `rules/dsl.py`: `compile(match) -> Predicate`, where `Predicate(fields) -> (bool, trace)` and `trace` lists every leaf with its actual value and pass/fail (the UI shows this).
   - Ops: CONTRACT §5. Numeric coercion. Severity order for `$severity` comparisons. `glob` via fnmatch. `regex` pre-compiled with a length cap. Missing field: `exists` false, everything else false.
   - Also accept the **flat `{path: value}` map** form (Appendix B runbook `matches`). DATA and DELIVERY import this compiler, so ship it first and keep its signature stable.
   - Unit tests for every op, plus the 4 rules × positive and negative cases from `fixtures/golden/expectations.yaml`.
2. **Engine (pure)** `rules/engine.py`: `evaluate(event, ruleset, state, clock) -> Evaluation`:
   - Per enabled rule: match → alert key (`group.by` / schema `alert_key` / fallback) → op `upsert` with a transition (`created`, `occurrence`, `severity_increased`, `cooldown_elapsed`) plus rendered presentation (`templates.render`: title, body, fields, colour).
   - `resolve_when` handling. `NO_MATCH` / `UNROUTED` decisions.
   - `state` is an interface (`get_open(rule_id, key) -> AlertSnapshot | None`) so the worker passes a DB-backed view and the simulator passes an in-memory dict.
3. **Rule-worker** `python -m alerting.rules.worker --lane normal|critical --batch 200`, one transaction per batch (CONTRACT §6 order):
   - Claim with `FOR UPDATE SKIP LOCKED` → evaluate → **pre-aggregate per (rule, key)** (`count += k` once per batch) → upsert alerts (`INSERT … ON CONFLICT (rule_id, alert_key) WHERE status <> 'resolved' DO UPDATE`).
   - Call DELIVERY's `routing.plan()` (until it lands: a stub returning one `ui` intent to the owner team) → insert notifications (`ON CONFLICT (dedup_key) DO NOTHING`), store `plan.runbook_id`, decisions, `alert_occurrences` (keep the last 50).
   - Mark events processed with `trace.processed_at`, then `NOTIFY alerts`.
   - Reload the ruleset on `NOTIFY config` (until DATA's loader lands, read `config/files.py`).
4. **Alerts API** `GET /v1/alerts?team=&status=&q=`, `GET /v1/alerts/{id}` (fields, group values, occurrences sample, score breakdown, timeline = decisions + audit; DELIVERY adds notifications/runbook via its own endpoints), `POST /ack`, `POST /resolve` (header `X-User`, audit row, `NOTIFY alerts`).

### By G2 (Sat 04:00)
5. **Scheduler** `python -m alerting.rules.scheduler` (every 2 s): `digest` alerts due (`every` elapsed and `count_since_notify > 0`) → `digest_due` → `routing.plan` → notifications; `cooldown_elapsed` re-notify for immediate alerts still receiving occurrences.
6. **Explain** `GET /v1/events/{id}/explain`: schema detection + each rule's trace + alert op + planned notifications + final outcome. This is the "why was / wasn't I notified" screen.
7. **Rules CRUD** `GET/POST /v1/rules`, `PUT /v1/rules/{id}` (validate by compiling; new version in `config_objects`, origin `ui`; file-origin rules are read-only, so return 409 with a hint), `POST /v1/rules/test {rule, event}` → trace. Hot reload proves itself: change a rule in the UI and the next event uses the new version (version shown in explain).
8. **Storm test:** 500 distinct events for one key across 2 workers → exactly 1 open alert, count 500, sam paged once (golden `storm-500-one-alert`).

### By G3 (Sat 07:30), P1 in this order
9. **Simulator** `POST /v1/rules/{id}/preview {draft, last_n: 2000}`: load the last N events' `fields` + `received_at`, replay in order through `evaluate()` twice (current vs draft) with in-memory state and `FixedClock(event.received_at)`, call `routing.plan()` for notification counts. Return a diff:
   - totals: alerts, notifications by recipient and channel;
   - events that **lost all notifications**, listed with severity (the "would we miss something" list);
   - new recipients;
   - sample event ids.
   **No DB writes** (add a test that counts rows before and after). State that it starts from empty state.
10. **NL → rule draft** `assist/rule_draft.py` + `POST /v1/rules/draft {text, sample_event_id?}`: `llm.structured(RuleDraft, …)` with the DSL spec, known fields (from the sample or recent events), teams and users. Validate by compiling. Return `{yaml, plain_english, warnings}`. Heuristic fallback: return a template with the sample's fields prefilled.
11. **Proposals (task 2.4 "Propose fixes")** `GET/POST /v1/proposals`, `POST /v1/proposals/{id}/approve|reject`: a human proposal (draft from the UI) or an LLM fix (`assist/fix_proposal.py`: given alert stats such as "fired 40× in 10 min, 3 distinct instruments", suggest grouping/digest/threshold changes plus a rationale). Each proposal stores its preview. Approving creates a new rule version (audit) and hot-reloads.
12. **Weighted score + lanes:** `score = severity (critical 50 / error 35 / warning 20 / info 5) + environment (prod 25) + rule impact (optional `impact:` 0–25) + frequency (min(15, 5·log10 count))`, stored with a breakdown. Used to sort the feed and pick the lane (`critical` if severity critical or score ≥ 70). **It never changes a rule's route.** Run a dedicated `--lane critical` worker.
13. P2: related-alerts (open alerts within ±5 min whose services are linked in `config/dependencies.yaml`), shown as "possibly related", with no causal claim.

## Acceptance (Yi checks with the golden runner)
All `settlements-*`, `rates-*`, `anna-*` (matching side), `pricing-digest`, `storm-500-one-alert`, `alertmanager-auto-resolve`, `rates-missing-template-field` PASS. Preview leaves the DB unchanged.

## Pitfalls
- Rule matches are **not exclusive**: one event can open alerts for several rules (Anna + a payments team rule).
- Don't group Settlements by `workflowId` (that turns 500 failures into 500 alerts). The rule uses `[workflowName, step, error.code]`.
- Never hold a DB transaction open while calling the LLM. LLM calls happen only in API endpoints (draft/proposal), never in the worker.

## Starter prompt for your coding agent
> You are implementing the RULES role of the Alert Funnel hackathon project. Read `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/RULES.md` fully first. Work only inside the directories listed under "You own" in RULES.md. Do not edit shared files (`contracts.py`, `llm.py`, `db.py`, `clock.py`, `templates.py`, `api/app.py`, `001_init.sql`); if one needs a change, stop and tell me. Start with task 1 (`rules/dsl.py`) test-first, using the four rules in `config/rules.yaml` and the positive and negative cases in `fixtures/golden/expectations.yaml`, then task 2 (pure engine) test-first, then the worker. Commit after each task and run `make test`. Python 3.12, psycopg 3 async. Report what you ran and what you did not run.
