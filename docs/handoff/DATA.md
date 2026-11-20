# Handoff — DATA (ingest, config, AI assist, simulators, measurement)

**Branch:** `data/platform` · **Layers:** L1 ingest, L2 validate/normalise, config + hot reload, all LLM assist, SSE, simulators, measurement
**You own:** `backend/alerting/ingest/`, `schemas/`, `config/`, `assist/`, `simulate/`, `api/routes_ingest.py`, `routes_schemas.py`, `routes_rules.py`, `routes_teams.py`, `routes_config.py`, `routes_stream.py`, `routes_ops.py`, `mocks/`, `loadtest/`, `tools/`, `docker-compose.yml`, `backend/Dockerfile`, `db/migrations/0xx_*.sql`
**You call (pure functions owned by BACKEND):** `rules.dsl.compile`, `rules.engine.evaluate`, `routing.plan`, `runbooks.render`. Until they land, use a local `exists/eq` matcher.
**Read first:** BRIEF §1–2, CONTRACT §2, §3, §4, §9 (your rows), §10, §11, §12

## Mission
Every event from any source, in any shape, lands durably in `events` with the right identity, schema, aliases and severity, or is visibly flagged. Teams change rules, schemas, preferences and runbooks at runtime. The LLM helps onboard unseen schemas and runbooks and draft rules, and is never required. The outside world (telecom, ops) is simulated independently so delivery can be measured. You produce the throughput and latency numbers.

## Tasks (in order; ★ = on the G1 critical path)

### By G1 (Sat 00:00)
1. ★ **Normaliser (pure, test-first)** `ingest/normalize.py`:
   - `flatten` (CONTRACT §3.2 limits) and `detect_schema` (DSL; first match).
   - Aliases `$schema/$severity/$service/$environment/$time/$source/$transport`.
   - Severity map → default → unknown; `time_format`.
   - `identity()` (CONTRACT §2 order); `payload_hash` = sha256 of canonical JSON.
   - Tests: the 4 fixtures produce the schemas and severities the golden file expects.
2. ★ **HTTP ingest** `POST /v1/events/{source_id}`:
   - API key from `sources.yaml`; single object, array, or `split_path`; size limits.
   - Multi-row `INSERT … ON CONFLICT` on the identity index: same id with a different `payload_hash` → `CONFLICT`, same hash → `DUPLICATE` (both recorded in `ingest_rejections`).
   - `trace = {gen_sent_at, received_at, demo_clock}`; lane hint (`$severity == critical` → `critical`).
   - Returns 202 + counts. Also `GET /v1/events/{id}`.
3. ★ **RabbitMQ consumer** `python -m alerting.ingest.amqp`: one consumer per source queue, prefetch 200, batch ≤ 200 or 50 ms → one transaction → **ack after commit** (`multiple=True`). `message_id` = identity; headers `x-gen-sent-at`, `x-demo-clock`. Plus `tools/publish.py --source … --file … [--count N --rate R --unique workflowId]`.
4. ★ **fake-telecom** `mocks/fake_telecom/` (FastAPI :8100):
   - `POST /sms`, `POST /call` (dedup by `Idempotency-Key`, records `received_at` + `X-Gen-Sent-At`); `POST /mode {ok|down|503|slow|drop_response}`; `GET /messages`.
   - `GET /` = a phone-like page per number, auto-refreshing, with a reply box that forwards to `POST {API}/v1/inbound/sms {from, body}`.
5. ★ **SSE** `GET /v1/stream?team=`: one `LISTEN` connection per API process on `alerts, notifications, actions, schema_issues, config` → per-subscriber queues, team filter, 15 s heartbeat, `stats.tick` every 2 s (CONTRACT §10). Agree on the NOTIFY payloads with Yi in 5 minutes.

### By G2 (Sat 04:00)
6. **Log tailer** `python -m alerting.ingest.logtail`: follow (truncate/rotate safe), regex + logfmt `kv_group`, batch insert. Pricing golden case.
7. **Unknown schema:** `needs_review` + upsert `schema_issues` by fingerprint (sorted top-level keys). For a **new** fingerprint, in the same transaction, insert a system alert (`rule_id = 'system:needs-review'`, team = `fallback_team`, grey) + one `ui` notification, then `NOTIFY schema_issues`.
8. **Config store + hot reload** `config/loader.py`: same function names as `config/files.py`, backed by `config_objects` (import files on start, origin `file`; new version only when `content_hash` changes); a `watchfiles` watcher re-imports → `NOTIFY config`.
9. **Config APIs (writes create a new version, origin `ui`; file-origin objects → 409 "edit the file"):**
   - `routes_schemas`: `GET /v1/sources`, `GET/POST /v1/schemas` (POST re-queues that fingerprint's `needs_review` events as `pending`), `GET /v1/schema-issues`.
   - `routes_rules`: `GET/POST /v1/rules`, `PUT /v1/rules/{id}` (validate with `dsl.compile`), `POST /v1/rules/test {rule, event}` → trace.
   - `routes_teams`: `GET/PUT /v1/teams`, `GET/PUT /v1/users/{id}/preferences`.
   - `routes_config`: `GET/POST /v1/runbooks` (any format, kept verbatim).
10. `GET /v1/stats/funnel`, `POST /v1/debug/reset` (DEMO_MODE), `/metrics`.

### By G3 (Sat 07:30): P0.5 first, then P1
11. **P0.5 schema suggestion** `assist/schema_suggest.py` + `POST /v1/schemas/suggest {sample}`:
    - Heuristic: name similarity for severity/level/priority, service/app/producer/instance/source, env/environment/stage, time/timestamp/ts/at; `event_id` = an `*id*` path + time; detect = the 2–3 most specific keys.
    - Then `llm.structured(SchemaSuggestion, …)` on the redacted sample + alias vocabulary.
    - The LLM fills only gaps or low-confidence fields. Returns `{draft, source, rationale}`. Cache per fingerprint.
12. **P0.5 runbook structuring** `assist/runbook_structure.py` + `POST /v1/runbooks/structure {text, sample_alert_id?}`:
    - LLM call with BACKEND's action `REGISTRY` (names, params, default approval) + the alert's fields → steps in the `runbooks.render` step shape.
    - **Validator:** unknown action → manual; `restart|failover|rollback|escalate|delete` → `approval: required`; params naming missing fields → flagged.
    - Heuristic fallback: split sentences and numbered lines into manual steps.
13. **P1 simulator** `simulate/preview.py` + `POST /v1/rules/{id}/preview {draft, last_n: 2000}`:
    - Replay the last N events (`fields`, `received_at`) in order through `engine.evaluate` twice (current vs draft), with an in-memory state and `FixedClock(event.received_at)`, plus `routing.plan` for counts.
    - Diff: alerts; notifications by recipient/channel; **events that lose all notifications** (with severity); new recipients.
    - **No DB writes** (test counts rows before and after). Say it starts from empty state.
14. **P1 NL rule draft** `assist/rule_draft.py` + `POST /v1/rules/draft {text, sample_event_id?}`: the LLM gets the DSL spec + known fields + teams/users → YAML → validate with `dsl.compile` → `{yaml, plain_english, warnings}`.
15. **P1 Propose fixes (task 2.4)** `GET/POST /v1/proposals`, `POST /v1/proposals/{id}/approve|reject`: human or LLM (`assist/fix_proposal.py`: alert stats → grouping/digest suggestion + rationale). The preview is stored; approving creates a new rule version (audit) and hot-reloads.
16. **mock-ops** `mocks/mock_ops/` (:8200, compose alias `counterparty-service`): `GET /health` (toggle), `POST /workflows/{id}/retry` (fail N times then succeed), `POST /services/{name}/restart`, `POST /tickets`.
17. **Load test + chaos** `loadtest/`:
    - HTTP via k6 `constant-arrival-rate` (report `dropped_iterations`); AMQP via `tools/publish.py --rate`.
    - Collector over fake-telecom `/messages` + the Mailpit API: ingest/s, processed/s, delivered/s, e2e p50/p95/p99 **with N**, backlog + oldest-pending age over time, failures. 1 vs 2 rule-workers. Raw JSON + machine spec in `loadtest/results/`.
    - Chaos: fake-telecom `down` 30 s under load → reconciliation (sent = delivered + failed + pending; lost = 0).
    - Plus the golden runner `loadtest/golden_runner.py` (black box, see the header of `fixtures/golden/expectations.yaml`).
18. **compose for judges:** `backend/Dockerfile`; services api, ingest-amqp, ingest-logtail, rule-worker (+critical), scheduler, delivery-worker, fake-telecom, mock-ops, frontend. `docker compose up` on a fresh clone; time it.

## Acceptance
Golden ingest parts, `retransmission-dedup`, `unknown-schema-visible`, `pricing-digest` (ingest side). `kill -9` the AMQP consumer between commit and ack (`FAULT_AFTER_COMMIT_BEFORE_ACK=1`) → the event count is unchanged after restart. SSE: two browsers update without refresh. Preview leaves the DB unchanged. LLM off (`ASSIST_ENABLED=0`) → every assist endpoint still answers (`source: heuristic`).

## Pitfalls
- Never ack before commit. Batch inserts. The identity index is partial (`where source_event_id is not null`).
- A body claiming `critical` only sets the lane hint. Rules decide routing.
- Every LLM call goes through `alerting/llm.py` (redaction, timeout, fallback). Never call the LLM from a worker.

## Starter prompt for your coding agent
> You are implementing the DATA role of the Alert Funnel hackathon project. Read `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DATA.md` fully first. Work only inside the directories listed under "You own". Do not edit shared files (`contracts.py`, `llm.py`, `db.py`, `clock.py`, `templates.py`, `api/app.py`, `001_init.sql`); if one needs a change, stop and tell me. Do task 1 (normaliser) test-first against `fixtures/events/*` and `fixtures/golden/expectations.yaml`, then tasks 2–5 in order (HTTP ingest, AMQP consumer, fake-telecom, SSE), committing after each and running `make test`. Python 3.12, FastAPI, psycopg 3 async, aio-pika. Report what you ran and what you did not run.
