# DEV2 — Ingestion & data (HTTP, RabbitMQ, normalisation, dedup, config)

**Branch:** `feat/ingestion` · **Layers:** L1 ingest, L2 validate/normalise, config + hot reload, LLM onboarding assist
**You own:** `backend/alerting/ingest/`, `schemas/`, `config/`, `assist/`, `simulate/`, `api/routes_ingest.py`, `routes_schemas.py`, `routes_rules.py`, `routes_teams.py`, `routes_config.py`, `tools/`, `tests/ingest/`, `db/migrations/1xx_*.sql`
**You call (pure, owned by others):** `rules.dsl.compile`, `rules.engine.evaluate` (DEV3), `routing.plan`, `runbooks.render` (DEV4). Until they land, use a local `exists`/`eq` matcher.
**Read first:** BRIEF §1–3, CONTRACT §2, §3, §4, §9 (your rows), §11, §12

## Mission
Every event from any source, in any shape, lands durably in `events` with the right identity, schema, aliases and severity, or is visibly flagged `needs_review`. Raw and flattened fields are always stored, so anyone (or an agent) can query them. Teams change schemas, rules, preferences and runbooks at runtime, with no restart.

## Tasks (★ = needed for G1)

### By G1 (Sat 00:00)
1. ★ **Normaliser (pure, test-first)** `ingest/normalize.py`:
   - `flatten` with the limits in CONTRACT §3.2.
   - `detect_schema` against `config/schemas.yaml` (DSL, first match).
   - Aliases `$schema $severity $service $environment $error_code $message $time $source $transport`.
   - Severity map → default → `unknown`; `time_format`.
   - `identity()` following the CONTRACT §2 order; `payload_hash` = sha256 of canonical JSON (sorted keys).
   - Tests: the four `fixtures/events/*` give the schema names and severities that `fixtures/golden/expectations.yaml` expects.
2. ★ **RabbitMQ consumer** `python -m alerting.ingest.amqp` (aio-pika):
   - One consumer per `rabbitmq` source queue, prefetch 200.
   - Batch ≤ 200 messages or 50 ms → one multi-row insert transaction → **ack after commit** (`multiple=True`).
   - Identity from AMQP `message_id`; headers `x-gen-sent-at`, `x-demo-clock` go into `trace`.
   - Lane hint: `$severity == critical` → `critical`.
3. ★ **Publisher tool** `tools/publish.py --source workflow-bus --file fixtures/events/workflow_failed.json [--count N --rate R --unique workflowId --message-id X]`. Used by everyone for demos, and by DEV1 for load.
4. ★ **HTTP ingest** `POST /v1/events/{source_id}`:
   - `X-Api-Key` checked against `sources.yaml`.
   - Body is an object, an array, or wrapped under `split_path`.
   - Multi-row `INSERT … ON CONFLICT` on the identity index: same id + same hash → `DUPLICATE`; same id + different hash → `CONFLICT`. Both are recorded in `ingest_rejections` and returned with the existing event id.
   - Returns 202 `{accepted, duplicates, conflicts, rejected[], event_ids}`.
   - Also `GET /v1/events/{id}`.

### By G2 (Sat 04:00)
5. **Log tailer** `python -m alerting.ingest.logtail`: follows the file (safe on truncate/rotate), parses with regex + logfmt `kv_group`, batch inserts. Covers the Pricing golden case.
6. **Unknown schema:**
   - No detect match → `status = needs_review`, and upsert `schema_issues` by fingerprint (sorted top-level keys).
   - For a **new** fingerprint, in the same transaction, insert one system incident (`rule_id = 'system:needs-review'`, team = `fallback_team`, grey) + one `ui` notification, then `NOTIFY schema_issues`.
7. **Event query API (investigation)** `GET /v1/events?source=&schema=&status=&field=<path>&value=<v>&limit=`. Uses the GIN index on `fields`. This is how a developer or an agent explores unstructured data.
8. **Config store + hot reload** `config/loader.py`:
   - Same function names as `config/files.py`, backed by `config_objects`.
   - Import files on start (origin `file`); create a new version only when `content_hash` changes.
   - A `watchfiles` watcher re-imports on change → `NOTIFY config`.
9. **Config APIs.** Writes create a new version with origin `ui`. Objects that come from files return 409 "edit the file".
   - `GET /v1/sources`
   - `GET/POST /v1/schemas` (POST re-queues `needs_review` events of that fingerprint) and `GET /v1/schema-issues`
   - `GET/POST/PUT /v1/rules` (validate with `dsl.compile`) and `POST /v1/rules/test {rule, event}` → trace
   - `GET/PUT /v1/teams` and `GET/PUT /v1/users/{id}/preferences`
   - `GET/POST /v1/runbooks` (any format, stored verbatim)

### By G3 (Sat 07:30): P0.5 first, then P1
10. **P0.5 schema suggestion** `assist/schema_suggest.py` + `POST /v1/schemas/suggest {sample}`:
    - Heuristic first: name similarity (severity/level/priority, service/app/producer/instance/source, env/environment/stage, time/timestamp/ts/at, code/error/reason, message/msg/summary); `event_id` = an `*id*` path plus time.
    - Then `llm.structured(...)`. The LLM fills only the gaps.
    - Returns `{draft, source: llm|heuristic, rationale}`. Cache per fingerprint.
11. **P0.5 runbook structuring** `assist/runbook_structure.py` + `POST /v1/runbooks/structure {text, sample_alert_id?}`:
    - The LLM maps text onto DEV3's action `REGISTRY` and outputs the step shape of DEV4's `runbooks.render`.
    - Validator: an unknown action becomes manual; `restart|failover|rollback|escalate|delete` force `approval: required`.
    - Heuristic fallback: sentences and numbered lines become manual steps.
12. **P1 rule simulator** `simulate/preview.py` + `POST /v1/rules/{id}/preview {draft, last_n}`:
    - Replays recent events through `engine.evaluate` twice (current vs draft) with in-memory state and `FixedClock(received_at)`, plus `routing.plan` counts.
    - Diff: incidents, notifications by recipient/channel, **events that lose all notifications**.
    - **No DB writes** (a test counts rows before and after).
13. **P1** NL rule draft `POST /v1/rules/draft` and proposals `GET/POST /v1/proposals`, `POST /v1/proposals/{id}/approve` (new rule version + audit).

## Acceptance
- Golden ingest parts pass, plus `retransmission-dedup` (the same message 1,000× → 1 event), `unknown-schema-visible`, `pricing-digest` (ingest side).
- `kill -9` the consumer between commit and ack (`FAULT_AFTER_COMMIT_BEFORE_ACK=1`) → the count is unchanged after restart.
- With `ASSIST_ENABLED=0`, the assist endpoints still answer (`source: heuristic`).

## Pitfalls
Never ack before commit. Always batch inserts. A body claiming `critical` only sets the lane hint; rules decide routing. Every LLM call goes through `alerting/llm.py`.

## Prompt for your coding agent
> Read `AGENTS.md`, `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DEV2-ingestion.md` fully. You are DEV2 (ingestion) on branch `feat/ingestion`. Work only inside the directories listed under "You own"; never edit shared files. If one needs a change, stop and tell me. Do task 1 test-first against `fixtures/events/*` and `fixtures/golden/expectations.yaml`, then tasks 2–4 (RabbitMQ consumer, publisher tool, HTTP ingest). Commit after each task, run `make test`, and push to `feat/ingestion`. Python 3.12, FastAPI, psycopg 3 async, aio-pika. Report what you ran and what you didn't.
