# Handoff — DATA (Data & Platform) · suggested owner: Yi

**Branch:** `data/platform` · **Layers:** L1 ingest, L2 validate/normalise, SSE, infra, measurement
**You own:** `backend/alerting/ingest/`, `schemas/`, `config/` (loader + watcher), `assist/schema_suggest.py`, `api/routes_ingest.py`, `routes_schemas.py`, `routes_stream.py`, `routes_ops.py`, `mocks/fake_telecom/`, `loadtest/`, `docker-compose.yml`, `backend/Dockerfile`, `db/migrations/0xx_*.sql`
**Read first:** BRIEF §1–2, CONTRACT §2, §3, §4, §9 (your rows), §10, §12

## Mission
Every event from any source, in any shape, lands durably in `events` with the right identity, schema, aliases and severity, or is visibly flagged. Everyone can see changes live (SSE). The outside world (SMS/phone gateway) is simulated independently so delivery can be measured. You also produce the throughput and latency numbers.

## Tasks (in order)

### By G1 (Sat 00:00)
1. **Normaliser (pure, unit-tested first).** `ingest/normalize.py`:
   - `flatten(obj) -> dict` (CONTRACT §3.2 limits).
   - `detect_schema(fields, schemas) -> schema | None` using the rule DSL. Until RULES' `rules/dsl.py` lands, implement `exists`/`eq` locally and switch later.
   - `apply_aliases()` → `$schema/$severity/$service/$environment/$time/$source/$transport`.
   - severity normalisation (`severity_map` → `severity_default` → `unknown`); `time_format` (iso / epoch_ms).
   - `identity(headers, schema, fields) -> source_event_id | None` (CONTRACT §2 order); `payload_hash` = sha256 of canonical JSON (sorted keys).
   - Tests: all four fixtures in `fixtures/events/` produce the schema names and severities that `fixtures/golden/expectations.yaml` expects.
2. **HTTP ingest** `POST /v1/events/{source_id}`: API key check against `sources.yaml`, single object or array (+ `split_path`), the limits, `INSERT … ON CONFLICT` on the identity index (DUPLICATE vs CONFLICT by `payload_hash`, written to `ingest_rejections`), `trace` = `{gen_sent_at, received_at, demo_clock}`, `lane` hint (`$severity == critical` → `critical`). One transaction per request (multi-row insert). Return 202 + counts. Also `GET /v1/events/{id}`.
3. **RabbitMQ consumer** `python -m alerting.ingest.amqp`: aio-pika, one consumer per source queue, prefetch 200, batch up to 200 messages or 50 ms → one transaction → **ack after commit** (`multiple=True`). Identity from `message_id`; headers `x-gen-sent-at`, `x-demo-clock`. Plus `tools/publish.py --source workflow-bus --file … [--count N --rate R --unique workflowId]` for demos and load.
4. **fake-telecom** (`mocks/fake_telecom/`, FastAPI, port 8100):
   - `POST /sms`, `POST /call` with `{to, text}` plus headers `Idempotency-Key`, `X-Gen-Sent-At`; dedups by key and records `received_at`.
   - Modes via `POST /mode {mode: ok|down|503|slow|drop_response}`: `down` = connection refused (stop accepting), `drop_response` = record then return 504.
   - `GET /messages` (JSON for the golden runner and load test).
   - `GET /` = a phone-looking page listing SMS and calls per number, live updated, with a reply box that forwards to `POST {API}/v1/inbound/sms {from, body}` (DELIVERY consumes it).
5. **SSE** `GET /v1/stream?team=`: one `LISTEN` connection per API process on channels `alerts, notifications, actions, schema_issues, config` → fan out to subscribers (asyncio queues), filter by team, 15 s heartbeat, and `stats.tick` every 2 s. The workers (RULES/DELIVERY) do the `NOTIFY` after commit with JSON payloads (CONTRACT §10). Agree on the payload shape with them in a 5-minute chat.
6. Commit `fixtures/api/` changes only if the UI asks for them. Keep `make infra` green.

### By G2 (Sat 04:00)
7. **Log tailer** `python -m alerting.ingest.logtail`: follows `sources.yaml` logfile paths (handles truncate/rotate), regex + logfmt `kv_group`, batch insert like AMQP. Pricing golden case.
8. **Unknown schema:** no detect match → `status = needs_review`, upsert `schema_issues` by fingerprint (sorted top-level keys). For a **new** fingerprint, insert one system alert (`rule_id = 'system:needs-review'`, team = `fallback_team`, colour grey) + one `ui` notification, in the same transaction, then `NOTIFY schema_issues`.
9. **Config loader with hot reload** `alerting/config/loader.py`: same function names as `config/files.py`, backed by `config_objects` (import files at start with origin `file`, new version only when `content_hash` changes), `watchfiles` watcher → re-import → `NOTIFY config, '{"kind":…, "id":…}'`. `POST /v1/schemas` (origin `ui`) → re-queue that fingerprint's `needs_review` events as `pending`.
10. `GET /v1/sources`, `GET /v1/schemas`, `GET /v1/schema-issues`, `GET /v1/stats/funnel` (raw, accepted, duplicates, needs_review, matched, alerts open, notifications by state, critical), `POST /v1/debug/reset` (DEMO_MODE only), `/metrics`.

### By G3 (Sat 07:30)
11. **P0.5 LLM schema suggestion** `assist/schema_suggest.py` + `POST /v1/schemas/suggest {sample}`:
    - Heuristic first: name similarity for severity/level/priority, service/app/application/producer/instance, env/environment/stage, time/timestamp/ts/at; candidate `event_id` = any `*id*` path + time; detect = the 2–3 most specific top-level keys.
    - Then `llm.structured(SchemaSuggestion, …)` with the redacted sample, our alias vocabulary and existing schema ids. Merge: the LLM wins only on fields the heuristic left empty or low-confidence. Return `{draft, source: llm|heuristic, rationale per field}`.
    - Cache by fingerprint.
12. **Load test + report** (`loadtest/`):
    - HTTP via k6 `constant-arrival-rate` (report `dropped_iterations`). AMQP via `tools/publish.py --rate`.
    - Collector reads fake-telecom `/messages` + Mailpit API; computes ingest/s, processed/s, delivered/s, e2e p50/p95/p99 **with N**, backlog and oldest-pending age over time, failures.
    - 1 vs 2 rule-workers on the same load. Save raw JSON to `loadtest/results/`. Write the machine spec in the report.
13. **Chaos script:** fake-telecom `down` for 30 s during load → back to `ok` → reconciliation (sent = delivered + failed + pending; lost must be 0).
14. **compose for judges:** `backend/Dockerfile`; add api, ingest-amqp, ingest-logtail, fake-telecom (+ DELIVERY's mock-ops, RULES' workers, UI's frontend). `docker compose up` from a fresh clone on another laptop; time it.

## Acceptance (Yi checks these with the golden runner)
- Golden: ingest parts of every case, `retransmission-dedup`, `unknown-schema-visible`, `pricing-digest` (the ingest side).
- `kill -9` the AMQP consumer between commit and ack (env `FAULT_AFTER_COMMIT_BEFORE_ACK=1`) → after restart, event count unchanged.
- SSE: two browsers see the same alert counter move without refresh.

## Pitfalls
- Never ack before commit. Never `json.loads` twice per message in the hot path. Batch inserts (a multi-row `VALUES` or `executemany` inside one transaction).
- Don't trust a body that claims to be `critical` for anything except the lane **hint**. Rules decide routing.
- The identity index is partial (`where source_event_id is not null`). Events without identity never conflict.

## Starter prompt for your coding agent
> You are implementing the DATA role of the Alert Funnel hackathon project. Read `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DATA.md` fully first. Work only inside the directories listed under "You own" in DATA.md. Do not edit shared files (`contracts.py`, `llm.py`, `db.py`, `clock.py`, `templates.py`, `api/app.py`, `001_init.sql`); if you think one needs a change, stop and tell me. Implement task 1 (normaliser) test-first against `fixtures/events/*` and the expectations in `fixtures/golden/expectations.yaml`, run `make test`, then continue with tasks 2–5 in order, committing after each task. Use Python 3.12, FastAPI, psycopg 3 (async), aio-pika. Report what you ran and what you did not run.
