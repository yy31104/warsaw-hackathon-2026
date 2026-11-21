# Shared Contract

The one page everyone codes against. Python types live in `backend/alerting/contracts.py`; the DB in `db/migrations/001_init.sql`. Change this file only through a PR that pings the whole team (BRIEF §7).

## 1. Glossary

| Term | Meaning |
|---|---|
| **event** | One message received from a source (one JSON body, one AMQP message, one log line, or one element of a `split_path` array) |
| **occurrence** | An accepted, non-duplicate event that matched a rule and was counted on an alert |
| **alert** (= *incident* in team talk) | One open problem = (rule, alert_key). Has a counter, first/last seen, status `open → acknowledged → resolved` |
| **notification** | One logical message to one recipient on one channel (outbox row). Can have many **attempts** |
| **action run** | One execution request of a runbook step (may wait for approval) |

## 2. The three identities

| Identity | Built from | Used for | Storage |
|---|---|---|---|
| **Event identity** `source_event_id` | 1) HTTP `Idempotency-Key` header or AMQP `message_id`; else 2) the schema's `event_id` template (team-declared, e.g. `{{workflowId}}\|{{step}}\|{{timestamp}}`); else 3) **none** (every message is a new occurrence) | Drop retransmissions. Same id + different `payload_hash` → `CONFLICT` (recorded, not overwritten) | `unique(source_id, source_event_id) where not null` |
| **Alert key** | `rule.group.by` values; else the schema's `alert_key` template; else `$schema\|$service\|$severity` | Count real occurrences. 500 failures = 1 alert, `occurrence_count = 500` | `unique(rule_id, alert_key) where status <> 'resolved'` |
| **Notification id** | UUID per (alert, kind, recipient, channel, epoch) via `dedup_key` | Retries reuse it. Sent to providers as `Idempotency-Key` / `X-Notification-Id` | `notifications.id`, `unique(dedup_key)` |

We never claim a payload hash perfectly separates retransmission from a genuine repeat. Hash-based dedup is opt-in per source (`dedup: payload_hash`) and labelled "heuristic" in the UI.

## 3. Canonical event and field references

### 3.1 Stored event (table `events`, model `CanonicalEvent`)
`id, source_id, transport(http|rabbitmq|logfile), source_event_id?, payload_hash, schema_id?, schema_version?, schema_name?, severity, service?, environment?, occurred_at?, received_at, raw (jsonb, redacted), fields (jsonb, flattened + aliases), lane(critical|normal), status(pending|processed|needs_review|rejected), trace (jsonb)`.

### 3.2 Flattening
Nested objects become dotted paths (`error.code`, `labels.service`, `payload.notional`). Arrays use indices (`items.0.id`); the whole array is also kept under its own path. Values keep their JSON types. Limits: body ≤ 256 KB, depth ≤ 20, ≤ 2,000 paths, otherwise `REJECTED` with a reason.

### 3.3 Aliases (prefixed `$`, computed at ingest)
`$source`, `$transport`, `$schema` (rendered `schema.name`), `$severity` (normalised), `$service`, `$environment`, `$error_code`, `$message`, `$time`. (These mirror the team's canonical event: source_id, service, environment, severity, error_code, message, occurred_at; everything else stays queryable under its raw path.) Rules may use raw paths **or** aliases.

### 3.4 Severity
Normalised enum `debug < info < warning < error < critical`, plus `unknown` (not comparable: `gte` on unknown is false). Mapping comes from the schema's `severity_map` (case-sensitive keys, then case-insensitive fallback); otherwise `severity_default`; otherwise `unknown`.

### 3.5 Templates
`{{path}}` over flattened fields and aliases (`{{payload.currency}}`, `{{$service}}`). A missing or null value renders as `‹missing: path›`. Substitution only, no expressions or code.

### 3.6 Unknown schema
If no `detect` matches: `status = needs_review`, the event is stored and counted, a `schema_issue` is recorded (keyed by the sorted set of top-level keys = the schema fingerprint), and **one** UI notification per new fingerprint goes to `fallback_team`. When a schema is saved, its pending events are re-processed.

## 4. Config formats (files in `config/`, versioned in table `config_objects`)

Kinds: `source`, `schema`, `rule`, `team`, `user`, `runbook`, plus singletons `fallback_team` and `default_preferences`. Each change creates a new version and triggers `NOTIFY config, '{"kind": "<kind>", "id": "<id>", "version": <n>}'` (channel name is exactly `config`). **One origin per object:** objects that come from files are read-only in the UI (`origin = file`). UI/API objects have `origin = ui`.

- **sources.yaml**: `id, transport, queue? | path? + parser? | api_key?, split_path?, dedup?(payload_hash)`.
- **schemas.yaml**: `id, detect (rule DSL), name (template), event_id?, aliases{severity,service,environment,time}, severity_map?, severity_default?, time_format?(iso|epoch_ms|epoch_s), alert_key?, resolve_when? (DSL)`.
  - `transforms?`: `{<path>: {scale: 0.01, unit: EUR}}`, applied after flattening and before rules (e.g. a notional sent in cents). The original value is kept as `<path>.raw`. If a rule compares a numeric field whose unit is unknown for this schema, onboarding flags it.
    Keys use the source value retained at `<path>.raw` for each declared unit transform. Rule comparisons and displayed group values use the scaled value.
  - `episode_key?`: a template identifying one firing episode (Alertmanager: `{{startsAt}}`); it is exposed as `$episode`. The alert stores its `latest_episode`. A resolve event closes the alert **only if its `$episode` equals the alert's `latest_episode`**. Otherwise the outcome is `STALE_RESOLVE` and the alert stays open, with the resolve kept as evidence.
- **rules.yaml**: see §5.
- **teams.yaml**: `fallback_team, default_preferences[], teams[{id, members, oncall{primary, secondary?}, approvers[], required_channels?: {critical: [phone, sms]}}], users[{id, timezone, contacts{email,sms,phone}, preferences[]?}]`. Preference row: `{when?: "HH:MM-HH:MM" (user tz, [start,end)), min_severity, channels[]}`. The first matching row wins. If no user row matches, `default_preferences` applies. **A critical alert always gets at least one channel.**
- **runbooks/runbooks.yaml**: `id, file, format (yaml-steps|markdown|checklist|text|url), rules[]?, matches?`. `matches` accepts the rule DSL **or** a flat `{path: value}` map (all-equal), which is the Appendix B YAML style.

## 5. Rules

```yaml
id: string
owner: "team:<id>" | "user:<id>"
enabled: true
description: plain English (UI shows it; generated if missing)
match: <DSL>
route: [{to: "user:<id>"|"team:<id>"|"oncall:<team>", channels: [email|sms|phone|ui] | preferences}]
present: {title: tmpl, body: tmpl, fields: [paths], color: red|orange|amber|blue|grey}
group: {by: [paths]?, mode: immediate|digest, cooldown: 10m, every: 5m, renotify_on_severity_increase: true}
throttle: {per_recipient: 5, per: 10m}          # optional; overflow → one "N more alerts" message
priority: P0..P3                                 # optional override; else critical→P0, error→P1, warning→P2, info→P3
```

**DSL:** `{all: [...]}`, `{any: [...]}`, `{not: {...}}`, leaf `{field, op, value?}`. Ops: `eq neq in not_in gt gte lt lte glob regex contains exists not_exists`. Numbers compare numerically (strings that parse as numbers are coerced). `gte/lte` on `$severity` use severity order. `regex` uses Python `re` on values ≤ 4 KB with a pre-compiled pattern ≤ 200 chars.

**Semantics:** an event is evaluated against **all** enabled rules (a rule match is not exclusive). For each match, upsert the alert by key: a new alert gives transition `created`, an existing one gives `occurrence` (count += 1). In `immediate` mode, notify on `created`, on `severity_increased`, and after `cooldown` has passed since the last notification. In `digest` mode, notify on `digest_due` every `every` with the count since the last digest. Matching no rule gives `NO_MATCH` (visible in explain, and counted as "unrouted" if severity ≥ error). An event matching a schema's `resolve_when` resolves the open alert(s) with the same alert key (`resolved_by = system`).

**Decision outcomes** (`decisions.outcome`, enum `Outcome`): `ACCEPTED DUPLICATE CONFLICT REJECTED NEEDS_REVIEW NO_MATCH NEW_ALERT EXISTING_ALERT SUPPRESSED_COOLDOWN SUPPRESSED_THROTTLE DIGEST_QUEUED NOTIFIED UNROUTED RESOLVED STALE_RESOLVE QUARANTINED RULE_FAULT`.

**Processing failures** (rule-worker):
- **Deterministic bad input** → the event becomes `status = quarantined` with `trace.processing_error`, and gets a `QUARANTINED` decision.
- **Transient errors** (connection lost, statement/lock timeout, serialization failure) → roll back the batch and retry it after a backoff. Never quarantine for these.
- **A rule that raises** for valid events → `RULE_FAULT` for that rule only. The rule is skipped and reported to the fallback team; all other rules keep running.
- A failing batch is bisected to find the offending event. Each event has a retry budget (`trace.attempts`, max 3) before it is quarantined.
- Optional hysteresis: rule `group.stable_for: 2m` closes only after the resolve evidence has held, with no newer episode, for that long.

## 6. Module interfaces (Python, see `contracts.py`)

```python
# DEV1 Yi — pure, no I/O. Used by the rule-worker AND by daizhongtian's simulator.
rules.engine.evaluate(event: CanonicalEvent, ruleset: Ruleset, state: AlertStateView, clock: Clock) -> Evaluation
#   Evaluation = decisions[] + alert_ops[] (upsert/increment/resolve with transition)

# DEV1 Yi — pure, no I/O. Called by the rule-worker INSIDE its transaction.
routing.plan(alert: AlertSnapshot, transition: Transition, rule: dict, event_fields: dict,
             directory: dict, runbooks: list[dict], recent_sends: dict, clock: Clock) -> Plan
#   Plan = intents[] + runbook_id (Yi's routing picks the runbook; the worker stores it on the alert)
#   recent_sends[(recipient, channel)] = count in the throttle window (queried by the worker)

# SHARED — templates (backend/alerting/templates.py): render(template, fields) with ‹missing: path›
# SHARED — config files (backend/alerting/config/files.py): sources() schemas() rules() directory() runbooks()
#          daizhongtian later adds alerting/config/loader.py (DB-versioned, hot reload) with the same function names.

# DEV1 Yi — providers (one class per channel; a real provider = one new class)
class ChannelProvider(Protocol):
    channel: Channel
    async def send(self, msg: RenderedMessage, contact: str, idempotency_key: str) -> SendResult

# DEV1 Yi — runbook actions (one class per action; new action = one file + registry entry)
class Action(Protocol):
    name: str; default_approval: Literal["none","required"]; params_schema: dict
    async def execute(self, params: dict, ctx: ActionContext) -> ActionResult

# SHARED — LLM helper (backend/alerting/llm.py). Returns None on timeout/error/refusal → caller falls back.
await llm.structured(OutputModel, system=..., user=..., timeout_s=20) -> OutputModel | None
```

The rule-worker transaction, in order: claim a batch of events (`FOR UPDATE SKIP LOCKED`, lane-filtered) → evaluate → pre-aggregate per alert key → upsert alerts → `routing.plan()` → insert notifications (`ON CONFLICT (dedup_key) DO NOTHING`) → insert decisions → mark events processed → `NOTIFY alerts '<alert_id>'` → COMMIT.

## 7. Database (owner of `001_init.sql`: DEV1 Yi, tech lead)

Tables: `config_objects, events, ingest_rejections, schema_issues, decisions, alerts, alert_occurrences (sampled, last 50 per alert), notifications, delivery_attempts, action_runs, audit_log, rule_proposals`. See the SQL file for columns. New tables or columns go in your migration range (`1xx` DEV1 Yi, `2xx` DEV2 daizhongtian; `001` is shared).

## 8. Channels and delivery

`email` (SMTP → Mailpit, `X-Notification-Id` and `X-Gen-Sent-At` headers), `sms` and `phone` (HTTP → fake-telecom `POST /sms`, `POST /call`), `ui` (delivered on insert; the feed is the UI channel). Rendering per channel: `title`, `body`, `short` (≤ 160 chars: title + short link `http://localhost:5173/a/<id>`), `color`, `fields`, `runbook_excerpt` (email/UI full; SMS link only; phone reads `short` + "press 1 to acknowledge").
Runbook action runs: `requested → pending_approval → approved → running → succeeded | failed | unknown | rejected`. An action call that times out is **`unknown` (needs verification)**, not failed. Every action call carries `Idempotency-Key: action-<run_id>`. Before any re-run, the result is checked at the receiver (`GET /executions/<key>` on mock-ops); a re-run needs an explicit operator confirmation.

Delivery states: `pending → sending → delivered | retrying | unknown | failed | fallback | cancelled`. A timeout after sending is `unknown`, retried with the **same** notification id. Backoff is `min(60s, 2^n s) ± 20 % jitter`, honouring `retry_after`. Max 8 attempts or 15 min → `failed` (visible), or `fallback` if a fallback channel is configured.

## 9. HTTP API (prefix `/v1`; owner in brackets)

| Method & path | Purpose | Owner |
|---|---|---|
| `POST /events/{source_id}` | Accepts JSON objects and arrays, NDJSON/JSONL, CSV with a header row, YAML mappings and lists, XML, and plain-text lines. `Content-Type` is a decoding hint. Headers include `X-Api-Key`, optional `Idempotency-Key`, `X-Gen-Sent-At` (epoch ms), and `X-Demo-Clock`. A valid batch returns `202 {accepted, duplicates, conflicts, rejected, event_ids}`. | DEV2 daizhongtian |
| `GET /events?source=&schema=&status=&field=&value=` · `GET /events/{id}` | Query/inspect stored events (raw + flattened, GIN-indexed) | DEV2 daizhongtian |
| `GET /events/{id}/explain` | Decision trace (schema detection, each rule's match trace, incident op, notifications planned) | DEV1 Yi |
| `GET /sources` · `GET/POST /schemas` · `GET /schema-issues` · `POST /schemas/suggest {sample}` | Registry and onboarding (`suggest` = heuristic + LLM, returns a draft) | DEV2 daizhongtian |
| `GET/POST /rules` · `PUT /rules/{id}` · `POST /rules/test {rule, event}` · `POST /rules/{id}/preview {draft, last_n}` · `POST /rules/draft {text}` · `GET/POST /proposals` · `POST /proposals/{id}/approve` | Rule CRUD (new version on write), match trace, simulator diff, NL draft, propose-fix flow | DEV2 daizhongtian (calls Yi's pure engine) |
| `GET /alerts?team=&status=&q=` · `GET /alerts/{id}` · `POST /alerts/{id}/ack` · `POST /alerts/{id}/resolve` | Feed and detail (incl. occurrences sample, priority + score breakdown, timeline) | DEV1 Yi |
| `GET /alerts/{id}/notifications` · `GET /notifications?state=` | Delivery state + attempts | DEV1 Yi |
| `GET /runbooks` · `POST /runbooks` (any format) · `POST /runbooks/structure {text}` (LLM) | Runbook store + structuring | DEV2 daizhongtian |
| `GET /alerts/{id}/runbook` | Rendered steps + run state | DEV1 Yi |
| `POST /alerts/{id}/actions {step_index}` · `POST /action-runs/{id}/approve` · `POST /action-runs/{id}/reject` | Execute, approve, audit | DEV1 Yi |
| `POST /inbound/{channel} {from, body}` | Reply-to-approve | DEV1 Yi |
| `GET/PUT /teams` · `GET/PUT /users/{id}/preferences` | Directory and preferences (config CRUD) | DEV2 daizhongtian |
| `GET /stream?team=` (SSE) | Live events, §10 | DEV2 daizhongtian |
| `GET /stats/funnel?window=15m` · `GET /health` · `GET /metrics` (Prometheus text) · `POST /debug/reset` (DEMO_MODE) | Dashboard + ops | DEV2 daizhongtian |
| `GET /assist/status` | `{enabled, provider, model}`; the UI shows "AI on" or "AI off (rules only)" | DEV1 Yi |
| `GET /alerts/{id}` additions | `pending_actions:[{run_id, action, requested_by, approvers, status, created_at}]` and `next_escalation:{at, level, targets, channels}` or `null` | DEV1 Yi |
| `POST /schemas/{id}/restore {version}` · `POST /rules/{id}/restore {version}` · `POST /runbooks/{id}/restore {version}` | Republish an old version as a new version (audited); list endpoints show at least the last 5 versions | DEV2 daizhongtian |
| `GET /stats/response?window=24h&team=` | `{n_alerts, tta_p50_s, tta_p95_s, mtta_s, mttr_s, ack_rate, unrouted, notifications_per_alert, no_action_rate, interruptions:[{user, count}]}`; time to awareness = event received → first delivered notification to a person; MTTA excludes unacknowledged alerts, MTTR excludes unresolved ones | DEV2 daizhongtian |
| `POST /rules/check {rule}` | Rule Safety Checker → `{scenarios_checked:[...], violations:[{scenario, requirement, problem, trace}]}` (pure function `rules.safety.check` by Yi). It checks against the rule's declared channels and the team's optional `required_channels`; it never flags a declared UI-only rule | DEV2 daizhongtian (route) |
| `POST /schemas/verify {schema, samples:[{event, expect: "notify"\|"quiet"\|{recipients:[...], channels:[...]}, note?}]}` | Sample-based verification → per sample: actual vs expected recipients and channels, deciding fields, pass/fail, unit warnings (uses Yi's engine + plan, no side effects) | DEV2 daizhongtian |

**HTTP decoding.** Use `application/json`, `application/x-ndjson` or `application/jsonl`, `text/csv`, `application/yaml`, `application/xml`, or `text/plain`. A valid JSON object or array works with any media type, including curl's default `application/x-www-form-urlencoded`. A successfully decoded, explicitly declared CSV, YAML, or XML body takes precedence. After checking for a complete JSON document, the endpoint treats `text/plain` as NDJSON when every nonempty line is a JSON object. Other plain text uses the source's configured parser. A source without a parser stores each nonempty line as `{"message": line}` in review. Log tailing also accepts lines containing JSON objects.

CSV values stay strings, including leading zeros. CSV supports a UTF-8 BOM, CRLF and quoted commas or newlines. XML attributes use `@name`, text uses `#text`, and repeated children are lists. An XML collection with an `<events>` or `<records>` root and only child records is a batch. XML DTDs, entities and external references are prohibited.

**HTTP errors and compatibility.** Without a media type, the endpoint recognizes a JSON document, consistent NDJSON, a header-like CSV with consistent row widths, marked YAML, or well-formed XML. An ambiguous body returns `415` with `tried` and a media-type hint. The request and the normalized size of each event are capped at 256 KiB. An oversized request returns `413`. Depth and path limits remain 20 and 2,000. Invalid rows are reported with a zero-based `index`. For additional formats, the rejection includes a one-based record `row`, a `field` path, and a `fix_example`. CSV, NDJSON and plain text include the physical `line`. YAML reports the parser's record start line. XML record rejections are index-only for location. The XML parser can report a document-level syntax error line. A batch that keeps valid records returns `202`. A non-JSON batch with all records rejected returns `400`. The existing JSON response envelope and batch statuses are preserved. Multipart uploads, spreadsheet files, headerless CSV and multi-document YAML are unsupported. Send an explicit media type for plain text and unmarked YAML.

**Problems and fixes, one shape everywhere.** `rules/check`, `schemas/verify`, `runbooks/structure` (for each manual step) and the runbook publish lint return problems as `{code, message, fix_example?, path?, suggestions?: [path]}`. `code` is a stable name: `missing_target`, `conditional`, `unknown_action`, `combined_steps`, `masked_value`, `unknown_field`, `no_timezone`, `unit_unclear`, `unmapped_severity`, `ambiguous_schema`, `unknown_recipient`, `no_manual_fallback`. `message` and `fix_example` are deterministic, so they work without an LLM. With assist on, the LLM may add `plain`: one plain-language sentence.

**Time and severity diagnostics.** Verification exposes `normalization` and `problems` per sample. Live events retain the same diagnostics in `trace`. With no configured time format, ISO timestamps and epoch seconds or milliseconds are auto-detected. Naive times are read in UTC and produce `no_timezone`. Severity synonyms are case-insensitive. Unconfirmed ranks such as `SEV1`, `P1` and `high` stay `unknown` and produce `unmapped_severity`. Onboarding can return a separate `suggested_severity_map`. A reviewer confirms it by publishing an explicit `severity_map`.

`POST /inbound/{channel}` requires the header `X-Provider-Secret` (shared secret `INBOUND_SECRET` in `.env`, sent by fake-telecom); a missing or wrong secret → 401. Failed code attempts are persisted per sender: 5 failures in 15 min → 429, audited.

Actor identity (demo-grade): header `X-User: <user id>` on mutating calls, checked against `approvers` / team membership. Write it in the README as "simulated auth".

## 10. SSE events (`GET /v1/stream`)

`event: <type>`, `data: <json>`. Types: `alert.created`, `alert.updated` (count/status/severity/score), `notification.updated` (state, attempts), `action.updated` (state, approver), `schema.issue`, `config.changed`, `stats.tick` (every 2 s, funnel numbers). Each payload carries `{id, team_id?, ts}` plus the changed fields. The UI re-fetches the detail on demand. Implementation: workers `NOTIFY <channel>, '{"type": "<sse type>", "data": {...}}'` after commit, on exactly these channels: `alerts`, `notifications`, `actions`, `schema_issues`, `config`; the API `LISTEN`s and fans out (works with several API instances).

**Coalescing:** the SSE server sends `alert.updated` counter changes at most every ~400 ms per alert. `alert.created`, severity/priority increases, `action.updated` and resolutions go out immediately. Clients refetch the detail after a reconnect.

**Delivery-risk fields** in `GET /stats/funnel` and `stats.tick`: `oldest_pending_s` per channel and lane, `backlog`, `retries_5m`, `delivery_target_s`, `at_risk` (true when oldest pending > target).

## 11. LLM usage (shared helper `backend/alerting/llm.py`)

- **Provider** from `ASSIST_PROVIDER` (`openai` for this team; `anthropic` stays supported), **model** from `ASSIST_MODEL`. Keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) live only in the local `.env`. Output is structured and validated into a Pydantic model. Interactive endpoints use an 8 s timeout and then return the heuristic result. Every LLM result records `source: llm` and `model`.
- **Measured, not assumed:** `loadtest/results/llm-eval-<commit>/` reports n/N accuracy, invented parameters, risky steps without approval (must be 0), latency and cost per model. Eval cases are synthetic and written by us; never the private holdout.
- **Default model: `gpt-6.2-sol`** (M3 eval, `loadtest/results/llm-eval-92f0e5c06bd9/`). Accuracy tied with `gpt-6-luna` on every split (final: 17/17 fields, 9/9 steps). Sol was faster on the final split, p50/p95 1.59/2.11 s vs 3.13/6.23 s, and Luna's p95 is close to the 8 s timeout. Sol costs about $0.0014 per call. The sample is small, so treat the tie as "both adequate", not "equal".
- **Never on the alert path.** Only onboarding, runbook structuring, rule drafts, fix proposals and summaries. On `None` (timeout/error/refusal) the endpoint returns the deterministic suggestion with `"source": "heuristic"`.
- **What to send for an event:** `json.dumps(llm.describe_sample(fields))` (path, type, redacted example). Never a raw JSON string: `redact()` on raw JSON can mask numbers such as epoch-ms timestamps and break the JSON. Use `redact()` only on free text.
- **Before sending:** redact emails, phone numbers, card-like digit runs, `Bearer`/`token=`/`password=` values, and truncate samples to 8 KB. Send at most 3 samples. Cache by schema fingerprint or text hash.
- Every suggestion is a **draft**: a human confirms in the UI before it becomes config (audit: `actor`, `source: llm|heuristic`).

## 12. Measurement hooks (for the load test; everyone must carry these through)

`X-Gen-Sent-At` (HTTP) / AMQP header `x-gen-sent-at` → `events.trace.gen_sent_at`, plus `trace.received_at`, `trace.processed_at` → copied into `notifications.rendered.meta.gen_sent_at` → sent as the `X-Gen-Sent-At` header to fake-telecom / in email headers → the receivers record `received_at`. E2E latency = receiver time − gen_sent_at. Load reports also include **decisions written per 1,000 events** and **SMS/phone sends per 1,000 events**.

DEMO_MODE fault hook (tests only): an event with `trace.demo_fault = "raise"` makes evaluation raise for that event, to demonstrate poison-event isolation.


### Following an event in PostgreSQL logs

After applying `db/migrations/111_correlation_logs.sql` to an existing database, filter the PostgreSQL log by event ID. With the supplied Compose service and jq, for example:

```sh
docker compose logs --no-color postgres | sed -n 's/^.*alert_funnel //p' | jq -c 'select(.event_id == 42)'
```

Each lifecycle record includes stage, state, event ID, alert ID, notification ID, run ID and delivery attempt. IDs appear as soon as the corresponding object exists. Routing decisions identify the event that caused a send. Retries and fallbacks keep that event even when a later occurrence updates the alert. Incident-wide actions use the first linked decision, and approval notifications include the run ID. Unavailable IDs are null. The records use the PostgreSQL `LOG` level and contain only IDs, state and attempt metadata.
