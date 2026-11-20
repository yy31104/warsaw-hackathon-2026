# Improvement backlog — everything we keep, in order

**All tiers are targets — we plan to complete every item.** *Target* means planned. An item is **done** only when its acceptance check PASSes on the integrated `main` and is recorded in `docs/GATES.md`; unit tests alone don't count. The order is the build sequence: **core chain → Tier 0 → Tier 1 → Tier 2 → Tier 3**. Nobody starts Tier 2/3 work in their lane until their part of the end-to-end chain (RabbitMQ → inbox → rules → outbox → fake-telecom → live UI) PASSes on `main`. Each person's ordered checklist is the "Full target list" at the end of their handoff.
Every claimed improvement needs a before/after measurement on the same input, with N.

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
