# DEV2 · daizhongtian — Data & platform (ingest, dedup, config, AI assist, live stream, infra, measurement)

**Branch:** `feat/data-daizhongtian` · **GitHub:** `daizhongtian`
**Layers:** L1 ingest, L2 validate/normalise, config + hot reload, LLM onboarding assist, SSE, simulators, measurement

**You own:**
- **Backend packages:** `backend/alerting/ingest/`, `schemas/`, `config/`, `assist/`, `simulate/`.
- **API routes:** `api/routes_ingest.py`, `routes_schemas.py`, `routes_rules.py`, `routes_teams.py`, `routes_config.py`, `routes_stream.py`, `routes_ops.py`.
- **Infra and tools:** `mocks/fake_telecom/`, `loadtest/` (everything **except** `golden_runner.py`), `tools/`, `docker-compose.yml`, `backend/Dockerfile`.
- **Not yours (moved to Yi):** `mocks/mock_ops/`, `loadtest/golden_runner.py`.
- **Tests and migrations:** `backend/tests/ingest/`, `db/migrations/2xx_*.sql`.

**You call (Yi's pure functions):** `rules.dsl.compile`, `rules.engine.evaluate`, `routing.plan`, `runbooks.render`. Until they land, use a local `exists`/`eq` matcher.

**Read first:** BRIEF §1–3, CONTRACT §2, §3, §4, §9 (your rows), §10, §11, §12.

## Mission
Every event, from any source and in any shape, lands durably in `events` with the right identity, schema, aliases and severity, or is visibly flagged `needs_review`. Raw and flattened fields are always stored, so anyone (or an agent) can query them. Teams change schemas, rules, preferences and runbooks at runtime. The UI updates live. The outside world (telecom, ops) is simulated independently so we can measure delivery. You produce the numbers.

## Tasks (★ = needed for G1)

### By G1 (Sat 00:00)
1. ★ **Normaliser (pure, test-first)** in `ingest/normalize.py`:
   - `flatten`, with the limits in CONTRACT §3.2.
   - `detect_schema`: first match in `config/schemas.yaml`.
   - Aliases: `$schema $severity $service $environment $error_code $message $time $source $transport`.
   - Severity: map → default → `unknown`. Plus `time_format`.
   - `identity()`, following the order in CONTRACT §2. `payload_hash` = sha256 of canonical JSON.
   - Tests: the 4 files in `fixtures/events/*` produce the schemas and severities the golden file expects.
2. ★ **RabbitMQ consumer** `python -m alerting.ingest.amqp` (aio-pika):
   - Prefetch 200. Batch up to 200 messages or 50 ms into one multi-row insert transaction, then **ack each message individually after the commit** (`await message.ack()` per delivery). Don't use `multiple=True`: with several queues on one channel it can also acknowledge another queue's messages that aren't committed yet.
   - Event identity = `message_id`. Headers `x-gen-sent-at` and `x-demo-clock` go into `trace`.
   - Lane hint: `$severity == critical` → `critical`.
3. ★ **Publisher tool** `tools/publish.py --source … --file … [--count N --rate R --unique workflowId --message-id X]`.
4. ★ **HTTP ingest** `POST /v1/events/{source_id}`:
   - `X-Api-Key` checked against `sources.yaml`. Body may be an object, an array, or wrapped in `split_path`.
   - `INSERT … ON CONFLICT` on identity: `DUPLICATE` vs `CONFLICT` (stored in `ingest_rejections`, returns the existing id).
   - Returns 202 with counts. Also `GET /v1/events/{id}`.
5. ★ **fake-telecom** `mocks/fake_telecom/` (FastAPI, port 8100):
   - `POST /sms` and `POST /call`: dedup by `Idempotency-Key`, record `received_at` + `X-Gen-Sent-At`.
   - `POST /mode {ok|down|503|slow|drop_response}` to inject failures.
   - `GET /messages`.
   - `GET /` shows a phone-like page per number with a reply box that forwards to `POST {API}/v1/inbound/sms`.
6. ★ **SSE** `GET /v1/stream?team=`:
   - One `LISTEN` connection on `alerts, notifications, actions, schema_issues, config`.
   - A queue per subscriber, filtered by team. 15 s heartbeat, `stats.tick` every 2 s (CONTRACT §10).
   - Agree the NOTIFY payloads with Yi.

### By G2 (Sat 04:00)
7. **Log tailer** `python -m alerting.ingest.logtail`: safe across truncate/rotate; regex + logfmt `kv_group`; batch insert. This is the ingest side of the Pricing golden case.
8. **Unknown schema:**
   - Mark the event `needs_review` and upsert `schema_issues` by fingerprint.
   - For a new fingerprint, in the same transaction: create a system incident (`system:needs-review`, `fallback_team`, grey) + one `ui` notification, then `NOTIFY schema_issues`.
9. **Event query API** `GET /v1/events?source=&schema=&status=&field=&value=` (uses the GIN index). This is how people and agents investigate unstructured data.
10. **Config store + hot reload** `config/loader.py`:
    - Same function names as `config/files.py`, backed by `config_objects`.
    - Import the files on start (`origin = file`). A new version is created only when `content_hash` changes.
    - Watch the files with `watchfiles` → re-import → `NOTIFY config`.
11. **Config APIs.** A write creates a new version with `origin = ui`. A write to a file-origin object returns 409.
    - `GET /v1/sources`
    - `GET/POST /v1/schemas` (POST re-queues the `needs_review` events of that fingerprint); `GET /v1/schema-issues`
    - `GET/POST/PUT /v1/rules` (validated with `dsl.compile`); `POST /v1/rules/test`
    - `GET/PUT /v1/teams`; `GET/PUT /v1/users/{id}/preferences`
    - `GET/POST /v1/runbooks` (stored verbatim)
11b. **Minimal "Propose fix" (task 2.4, P0):** `POST /v1/proposals {alert_id, text}` stores the text, the author (`X-User`), the alert's `rule_id` and `base_version` in `rule_proposals` (`source = human`, `draft = {}`). `GET /v1/proposals?rule_id=&status=`. Previews, LLM suggestions and approval come in P1 (task 19).
12. **Ops** (`routes_ops`): `GET /v1/stats/funnel`, `POST /v1/debug/reset` (DEMO_MODE only), `/metrics`.
13. **mock-ops** `mocks/mock_ops/` (port 8200, network alias `counterparty-service`):
    - `GET /health` (toggleable)
    - `POST /workflows/{id}/retry` (fails N times, then succeeds)
    - `POST /services/{name}/restart`
    - `POST /tickets`
14. **Golden runner** `loadtest/golden_runner.py`:
    - Black box: public API + fake-telecom `/messages` + Mailpit API.
    - Reset between cases.
    - Writes a PASS/FAIL table for `docs/GATES.md`.

### By G3 (Sat 07:30): P0.5 first, then P1
15. **P0.5 schema suggestion** `assist/schema_suggest.py` + `POST /v1/schemas/suggest`:
    - Name-similarity heuristic first, then `llm.structured(...)` fills only the gaps. Send it `json.dumps(llm.describe_sample(fields))`, never the raw event JSON.
   - Keep an eval set (8–10 samples with expected aliases) in `backend/tests/ingest/` and report accuracy as n/N.
    - Returns `{draft, source, rationale}`. Cached per fingerprint.
16. **P0.5 runbook structuring** `assist/runbook_structure.py` + `POST /v1/runbooks/structure`:
    - Maps the text onto Yi's action `REGISTRY`, using Yi's step shape.
    - Unknown action → manual. Any of `restart|failover|rollback|escalate|delete` → `approval: required`.
    - Heuristic fallback when the LLM isn't available.
17. **Load + chaos** in `loadtest/`:
    - HTTP load with k6 `constant-arrival-rate` (report `dropped_iterations`); AMQP load with `tools/publish.py --rate`.
    - Collector reports: ingest/s, processed/s, delivered/s, e2e p50/p95/p99 **with N**, backlog over time, failures. Compare 1 vs 2 rule-workers.
    - Chaos: fake-telecom `down` for 30 s → reconciliation must show 0 lost.
    - Commit raw JSON + machine spec.
18. **Compose for the judges:**
    - `backend/Dockerfile`, plus services: api, ingest-amqp, ingest-logtail, rule-worker ×2 + scheduler + delivery-worker (Yi's commands), fake-telecom, mock-ops, frontend.
    - Time `docker compose up` from a fresh clone.
19. **P1:**
    - Rule simulator `POST /v1/rules/{id}/preview`: replay through Yi's pure engine + `routing.plan` in memory, **no DB writes**. Returns the diff, including events that lose all notifications.
    - NL rule draft `POST /v1/rules/draft`.
    - Proposals `GET/POST /v1/proposals` + approve.

## Acceptance
- Golden ingest parts pass: `retransmission-dedup` (same message 1,000× → 1 event), `unknown-schema-visible`, `pricing-digest` (ingest side).
- `kill -9` the consumer between commit and ack (`FAULT_AFTER_COMMIT_BEFORE_ACK=1`) → the count is unchanged.
- Two browsers update live.
- `ASSIST_ENABLED=0` → the assist endpoints still answer (heuristic).

## Pitfalls
Never ack before commit. Batch inserts. A body claiming `critical` only sets the lane hint. Every LLM call goes through `alerting/llm.py`.

## Prompt for your coding agent
> Read `AGENTS.md`, `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DEV2-data-daizhongtian.md` fully. You are DEV2 (data & platform) on branch `feat/data-daizhongtian`. Work only inside the directories under "You own"; never edit shared files (if one needs a change, stop and tell me). Do task 1 (normaliser) test-first against `fixtures/events/*` and `fixtures/golden/expectations.yaml`, then tasks 2–6 in order (RabbitMQ consumer, publisher, HTTP ingest, fake-telecom, SSE). Commit after each task, run `make test`, push to `feat/data-daizhongtian`. Python 3.12, FastAPI, psycopg 3 async, aio-pika. Report what you ran and what you didn't.

## Round 2 (added Fri 21:30) — after your G1 items work
The core chain comes first: RabbitMQ → inbox, fake-telecom and SSE are still the critical path. Then, in this order (CONTRACT §4, §9, §10, §12):

- **R1 fake-telecom reply forwarder** sends `X-Provider-Secret: $INBOUND_SECRET` to `POST /v1/inbound/sms` (Yi now rejects requests without it).
- **R2 mock-ops idempotency:** every action endpoint deduplicates by `Idempotency-Key`. Add `GET /executions/<key>`, which returns whether that call happened and its result, so Yi can verify an `unknown` action before any re-run.
- **R3 Normaliser:**
  - apply schema `transforms` (`scale`, `unit`; keep `<path>.raw`);
  - expose `$episode` from `episode_key`;
  - flag numeric fields used by rules that have no known unit.
- **R4 `POST /v1/schemas/verify` (headline innovation):** draft schema + samples marked notify/quiet, or with expected `{recipients, channels}`, → run Yi's `evaluate` + `plan` with no side effects → per sample: recipients, channels, deciding fields, pass/fail, unit warnings. Golden case `verify-unit-change`.
- **R5 `POST /v1/rules/check`** → Yi's `rules.safety.check`. Golden case `rule-safety-counterexample`.
- **R6 Delivery-risk metrics** in `/v1/stats/funnel` and `stats.tick`: `oldest_pending_s` per channel/lane, `backlog`, `retries_5m`, `delivery_target_s` (env `DELIVERY_TARGET_S`, default 30), `at_risk`. Also expose them on `/metrics`.
- **R7 SSE coalescing:** `alert.updated` counters at most every ~400 ms per alert; created, critical, approval and resolved are sent immediately; the client refetches on reconnect.
- **R8 Schema-drift flag:** if ≥ 20 recent events of a known schema lack a field that an enabled rule uses → `schema_issues` entry of kind `drift`, with the affected rules listed.
- **R9 Experiments** (same input, before and after; report N, machine spec and raw JSON):
  - (a) hot-key storm and (b) wide variety, each with critical events injected every second. Report ingest/s, processed/s, critical e2e p50/p99, failures, oldest pending, decisions per 1,000 events and SMS per 1,000 events;
  - (c) poison-event injection;
  - (d) provider outage reconciliation (lost = 0).
- **Then your Tier 2 items** in [IMPROVEMENTS.md](../IMPROVEMENTS.md), in this order: #7 RabbitMQ hardening (DLQ, delivery limit, publisher confirms, reconnect), #8 admission control + per-source quotas, #9 LLM guardrails, #6 graceful shutdown (consumers), #10 `sensitive` fields in schemas, #11 correlation IDs, #12 canary sender, #14 feedback → proposals.
- **P2 (only after everything above):** Incident Investigator `assist/investigate.py`.
  - Read-only, runs async after the incident is created, ≤ 3 calls to allow-listed mock-ops GET endpoints (`/health`, `/metrics/pool`, `/deployments`), each returning clearly labelled simulated data.
  - Never uses the action registry. Stores {tool, time, result, unknowns} on the alert.
  - The LLM only summarises the evidence it actually received.

## Full target list (all required, in order) — updated Fri 22:00
**Please push work in progress to `feat/data-daizhongtian` now and every hour.** Nothing has reached GitHub yet, and your ingest + fake-telecom + SSE are the critical path for everyone.

**Moved to Yi** (don't build these): mock-ops (task 13), golden runner (task 14), the Tier 2 #12 canary, the Tier 3 #20 incident memory. Yi also writes `rules.safety.check`; you only expose its route.

**Core (Round 1)**
- ☐ 1 normaliser
- ☐ 2 RabbitMQ consumer
- ☐ 3 publisher tool
- ☐ 4 HTTP ingest
- ☐ 5 fake-telecom
- ☐ 6 SSE
- ☐ 7 log tailer
- ☐ 8 unknown schema
- ☐ 9 event query API
- ☐ 10 config store + hot reload
- ☐ 11 config APIs
- ☐ 11b minimal Propose fix
- ☐ 12 ops endpoints
- ☐ 15 schema suggestion (LLM)
- ☐ 16 runbook structuring (LLM)
- ☐ 17 load + chaos
- ☐ 18 full compose
- ☐ 19 simulator, NL rule draft, proposals

**Round 2**
- ☐ R1 provider secret in fake-telecom replies
- ☐ R3 normaliser `transforms` + `$episode`
- ☐ R4 `/schemas/verify` (incl. expected recipients/channels)
- ☐ R5 `/rules/check` route
- ☐ R6 delivery-risk metrics
- ☐ R7 SSE coalescing
- ☐ R8 schema-drift flag
- ☐ R9 experiments (a)–(d)
- (R2 mock-ops idempotency is now Yi's.)

**Tier 2**
- ☐ #7 RabbitMQ hardening
- ☐ #8 admission control + per-source quotas
- ☐ #9 LLM guardrails
- ☐ #6 graceful shutdown (consumers)
- ☐ #10 `sensitive` fields in schemas
- ☐ #11 correlation IDs
- ☐ #14 "no action needed" → proposals

**Tier 3**
- ☐ #17 Incident Investigator (read-only tools + LLM summary of real tool output only)
- ☐ #18 shadow-rules API
- ☐ #19 request-totals source for SLO priority (labelled if simulated)
- ☐ #21 webhook HMAC per source

**Measure first:** run the measurements (COPY vs multi-row insert, GIN index cost, decisions per 1,000 events) and hand the evidence to Yi.
