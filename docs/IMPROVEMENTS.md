# Improvement backlog — everything we keep, in order

**All tiers are targets — we plan to complete every item.** *Target* means planned. An item is **done** only when its acceptance check PASSes on the integrated `main` and is recorded in `docs/GATES.md`; unit tests alone don't count. The order is the build sequence: **core chain → Tier 0 → Tier 1 → Tier 2 → Tier 3**. Nobody starts Tier 2/3 work in their lane until their part of the end-to-end chain (RabbitMQ → inbox → rules → outbox → fake-telecom → live UI) PASSes on `main`. Each person's ordered checklist is the "Full target list" at the end of their handoff.
Every claimed improvement needs a before/after measurement on the same input, with N.

## Round 3 — mentor feedback (Sat 01:00). Do these first; open every PR by 06:00
Source: a Goldman Sachs engineer at the venue (notes in BRIEF §9). Inside each person's list the order is the priority. After 06:00 only fixes and integration; W0 runs the final checks on `main` before G3 (07:30).

| # | Item | Owner | Acceptance (record in GATES.md) |
|---|---|---|---|
| M1 | **Cross-person approval:** the alert/runbook read returns pending action runs; the team's pending-approval count is exposed | Yi + zhaoqinghe | Sam requests an action; Lee approves it from a fresh browser session; Sam sees `approved_by=lee` and the execution count; the home-page count matches the database |
| M2 | **LLM provider adapter:** `ASSIST_PROVIDER=openai\|anthropic`, model from `ASSIST_MODEL`, `GET /v1/assist/status` | Yi | With `OPENAI_API_KEY` set, `/schemas/suggest` returns `source: llm` and the model; without a key it returns `source: heuristic`; the status endpoint says which |
| M3 | **LLM eval:** our own synthetic unseen events and runbooks; rules-only vs two models | Yi | Report n/N field mappings, n/N runbook steps, invented parameters (target 0), risky step without approval (**must be 0**), p50/p95 latency, cost per call. The default model is chosen from these numbers |
| M4 | **"Did you mean":** a rule field path that no recent event has gets up to 3 closest known paths | Yi | `errorCode` suggests `error.code`; `xyz` gets none; nothing is auto-corrected |
| M5 | **"What happens next"** on an alert: the next escalation step, its targets, channels and due time | Yi | Alert detail shows the next step and when; after ack it shows none |
| M6 | **More input formats over HTTP:** JSON object/array, NDJSON, CSV with a header row, YAML, XML (safe parser). Content-Type is sniffed when missing | daizhongtian | One sample per format becomes the same canonical event; malformed input gets a clear 4xx reason; an XML entity-expansion payload is rejected |
| M7 | **Forgiving fields:** time-format auto-detection (ISO, epoch s/ms, `YYYY-MM-DD HH:MM:SS`) with a no-timezone warning; built-in severity synonyms (sev1/P1/crit/fatal → critical, err → error, warn → warning) when `severity_map` has no entry | daizhongtian | One test per variant; verify marks the value "auto-detected" |
| M8 | **One problem shape everywhere** (CONTRACT §9): runbook structuring says why a step stayed manual; schemas/verify and rules/check use the same shape | daizhongtian (+ Yi for rules/check) | `restart the service` → `missing_target` with a fix example; an `if …` step → `conditional` |
| M9 | **Runbook lint at publish:** unknown action, missing params, unknown user/team/phone → rejected with field-level messages; no manual workaround or escalation step → warning | daizhongtian | One test per case |
| M10 | **Restore a previous version** of a schema, rule or runbook; keep at least the last 5 visible | daizhongtian + zhaoqinghe | Restoring v2 creates v4 equal to v2, audited; the next event uses it |
| M11 | **Saturation test:** offered load 700 → 1,400 → 2,800 → 5,600 events/s; 1, 2 and 4 normal workers; one hot key vs 1,000 keys. Record sustained processed events/s, p50/p99, backlog trend and dropped; find the knee and the bottleneck | daizhongtian | `loadtest/results/saturation-<commit>/` with the machine spec and the command |
| M12 | **`docs/SCALING.md`:** what scales out today, what is shared (PostgreSQL), the measured curve, the plan for 10×/100×/1,000× | Yi | Every number cites a result file or says "not measured" |
| M13 | **Response metrics (PagerDuty-style):** MTTA, MTTR, ack rate, interruptions (SMS + phone per responder) | daizhongtian (API) + zhaoqinghe (tiles) | The numbers match a SQL check over the same window |
| M14 | **Replay storm, measured only:** 1,000 `needs_review` events → publish the schema → count the notifications | daizhongtian | The count and the rules used are recorded |
| M15 | **Frontend on integrated main:** assist on/off badge; runbook original vs structured steps (kind, approval, source, reason); problems with fix examples; operations-console alert list; response-metric tiles; restore-version button | zhaoqinghe | The browser scenarios pass on integrated `main` |
| M16 | **Forgiveness matrix:** the README example with 8 deviations (renamed field, upper-case severity, epoch time, extra nesting, CSV, XML, no timezone, a typo in a rule field) → the result of each | W0 (Yi), on final `main` | Table in GATES.md and the README |

Nice-to-have, only after all of the above: a "don't notify on replay" switch or watchers; an acknowledgement timeout that re-triggers.

## Tier 0 — P0 hardening (BRIEF §4, Yi)
Poison-event isolation · approval security · DB timeouts · stale recovery never closes a newer episode · action timeout = `unknown`.

## Tier 1 — P1 innovations (BRIEF §4)
Sample-based source verification · Rule Safety Checker · impact preview + AI proposals · delivery-risk metrics · SSE coalescing · schema-drift flag · recovery hysteresis.

**Correction for the Rule Safety Checker (replaces "critical must reach a non-UI channel").** The task allows UI-only rules, so the checker only flags violations of what is *declared*:
- **(a) Explicit channels.** If a rule lists channels (Settlements: `[phone, sms]`), every matching scenario must deliver *all* of those channels to ≥ 1 person with a usable contact.
- **(b) Preferences.** If a rule routes by preferences, a critical match must still produce ≥ 1 channel (the CONTRACT §4 invariant). UI-only is valid when the rule or the team declares it.
- **(c) Team policy (optional).** `teams.yaml` may declare `required_channels: {critical: [phone, sms]}` per team. The checker enforces it for that team's rules.
- **(d) Always.** Every route target resolves to ≥ 1 user, and a rule that matches no known-schema sample gets the warning "never fires".

## Tier 2 — high value, low cost (adopted; after your Tier 0–1 items)
| # | Improvement | Real situation it handles | Owner | Acceptance |
|---|---|---|---|---|
| 1 | **Maintenance windows / silences** (matcher + expiry; decision `SILENCED`) | Planned work pages people for nothing | Yi (engine) + zhaoqinghe (UI) | A silenced event shows "Silenced by X until T" in explain; a critical event outside the matcher still pages |
| 2 | **Unacked escalation** (`escalate_after: 10m` → secondary on-call, then team lead) | The on-call person is asleep or offline | Yi (scheduler) | Without an ack, the secondary is notified after T; an ack stops the chain |
| 3 | **Heartbeat / deadman rule type** (`expect: {every: 5m, key: ...}`) | A workflow fails silently: no error, just no "completed" event | Yi (scheduler) | A missing heartbeat creates an alert; the next heartbeat resolves it |
| 4 | **Late events never reopen an old episode** (mirror of stale recovery) | A delayed failure from episode T1 arrives after T1 was resolved | Yi | It is recorded as a late occurrence; no new page |
| 5 | **DST-safe time windows** | Anna's 08:00–20:00 on 29 Mar / 25 Oct (Europe/Warsaw) | Yi | Tests at the DST boundaries pass, using `zoneinfo` |
| 6 | **Graceful shutdown** (SIGTERM: stop claiming, finish the batch or send, release leases; consumers nack-requeue unprocessed messages) | Deploys and restarts mid-traffic | Yi (workers), daizhongtian (consumers) | Restarting under load loses 0 events and 0 notifications (reconciliation) |
| 7 | **RabbitMQ hardening:** quorum queues + `x-delivery-limit` + dead-letter exchange for undecodable or oversize messages; publisher confirms in `publish.py`; consumer reconnects with backoff | Broker-level poison messages and broker restarts | daizhongtian | An undecodable message ends in the DLQ after N tries; a broker restart → the consumer resumes, nothing is lost |
| 8 | **Admission control + per-source quotas** (token bucket; HTTP 429 + `Retry-After` when backlog / oldest pending is over a threshold; critical sources last to be throttled) | A noisy source starves everyone; traffic beyond capacity | daizhongtian | During a flood, other sources keep their latency; accepted events are never dropped |
| 9 | **LLM guardrails:** concurrency cap, budget cap, a breaker that falls back to heuristics for 5 min after 3 failures, cache per fingerprint | The LLM provider is slow, down or expensive | daizhongtian | With the LLM down, onboarding still answers ("heuristic"); the call count stays bounded under a storm of new formats |
| 10 | **Sensitive fields masked in notifications** (schema `sensitive: [paths]`; masked in SMS/email, visible in the UI to authorised users) | Account numbers and PII leaking into SMS | daizhongtian (schema) + Yi (render) | A sample with an account number → the SMS shows `****1234` |
| 11 | **Correlation IDs in logs** (`event_id`, `alert_id`, `notification_id`, `run_id` on every log line) | "What happened to this event?" at 3 a.m. | everyone | One grep over the logs follows a single event end-to-end |
| 12 | **End-to-end canary** (a synthetic event every 60 s over HTTP → … → a reserved canary number on fake-telecom; never a human; on a miss, a bypass alert that doesn't use our outbox) | Our own alerting is broken and nobody knows | Yi (sender + bypass) | Stop the delivery worker → the canary alarm fires through the bypass within the target time; continuous e2e latency is visible |
| 13 | **Dynamic escalation with guards** (opt-in `escalate_on_spike`; requires count ≥ N absolute **and** ≥ k × EWMA baseline; raises priority and re-notifies) | Warnings that are suddenly getting much worse | Yi | A spike from 2/min to 200/min escalates; a low-traffic jump from 1 to 3 does not |
| 14 | **"No action needed" feedback → suggestion** (a resolve reason; after N of them, propose a digest with an impact preview; a human approves) | Rules that wake people up for nothing | daizhongtian + zhaoqinghe | After 10 "no action" resolves → a proposal with a preview appears |

## Tier 3 — valuable, needs more design (after Tier 2, or as P2)
| # | Improvement | Owner | Guardrail |
|---|---|---|---|
| 15 | Cross-rule notification merging | Yi | Opt-in `merge_group`; same recipient + channel + event only; never delays a critical alert; each alert keeps its own notification link and audit |
| 16 | Cost ladder (UI → email → SMS → phone) | Yi (+ zhaoqinghe display) | Opt-in per rule; never overrides a rule's explicit channels; stops on ack |
| 17 | Incident Investigator | daizhongtian (tools + LLM) + zhaoqinghe (panel) | Read-only, async, ≤ 3 allow-listed calls, simulated data labelled, never the action registry |
| 18 | Shadow rules (live dry-run) | Yi (engine state) + daizhongtian (API) + zhaoqinghe (diff view) | Separate state for counts and cooldowns; never sends |
| 19 | SLO / error-budget priority | daizhongtian (request totals source) + Yi (scoring) + zhaoqinghe | Only with real request totals (a denominator); simulated totals labelled |
| 20 | Incident memory cards | Yi (store + lookup) + zhaoqinghe (hint card) | Only human-confirmed resolutions; old cases are hints, not answers |
| 21 | Webhook HMAC signatures per source | daizhongtian | Optional per source; rotate via config |
| 22 | Fair claiming across teams/sources | Yi | Claim round-robin by source/team; prove with the noisy-neighbour load test |

## Measure first (the measurement is a target; the change follows the evidence)
Owner of the measurements: daizhongtian (load tests). Owner of any resulting backend change: Yi.
COPY staging tables · GIN index changes · compacting `NO_MATCH` decisions · partitioning or retention · Redis. The trigger is a load-test result showing the bottleneck; the change must keep "why wasn't I notified" answerable and must never delete audit history.
