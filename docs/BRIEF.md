# Alert Funnel — Team Brief

GS Warsaw Hackathon 2026 · Software Engineering · "Flexible Alerting & Notification Service"
Status: G0 draft, Fri 2026-11-20 ~20:30 CET · Brief owner: Yi (captain, final acceptance)

**Read order (15 min):** this brief → [CONTRACT.md](CONTRACT.md) → your handoff in [handoff/](handoff/).

---

## 1. What we are building

A service that ingests events from many sources and formats (RabbitMQ, HTTP webhooks, a tailed log file), lets each team write rules on **any field**, and delivers **the right alert to the right people on the channel they prefer** (email / SMS / phone / live UI). It doesn't flood people: duplicates are collapsed, real occurrences are counted and grouped, and sends are throttled. It then helps them **resolve** the alert with runbooks whose steps can be executed, with human approval for risky steps and a full audit trail.

The judges will bring an **unseen event schema and an unseen runbook** to the demo (Appendix A/B). We onboard both **without code**: the UI shows the gap, an LLM suggests the mapping or structure, and a human confirms.

**One story for the 5-minute video:** a settlement workflow starts failing in prod. 500 failures become one red alert with a counter. The on-call gets a phone call and an SMS. The runbook (filled in with the real workflowId) offers "retry". The retry fails twice, so the escalation waits for the lead's approval, given by SMS reply. Meanwhile a new team plugs in an unseen schema live, and an on-call proposes a quieter rule with a preview of its impact. Then we show the measured throughput and p50/p99.

## 2. Non-negotiables

1. **Durable before ack.** An event is in the `events` inbox (committed) before HTTP returns 202 or RabbitMQ gets its ACK. A notification is in the `notifications` outbox (same transaction as the alert change) before anyone tries to send it.
2. **Three identities, never mixed.** *Event identity* (a retransmission is not a new fact) ≠ *alert key* (500 real failures = 1 alert, count 500) ≠ *notification id* (3 send attempts = 1 notification).
3. **Deterministic core.** Matching, routing and delivery never wait for an LLM. The LLM *suggests* (schema mapping, runbook structure, rule drafts, fixes, summaries). A human confirms, and a deterministic fallback exists when it is slow, down or refuses.
4. **Nothing silently dropped.** An unknown schema becomes `needs_review` and goes to the fallback team. A missing template field renders as `‹missing: path›`. User preferences can never fully cancel a critical alert: if no preference covers it, the team default applies.
5. **Measured, not estimated.** Every number in the README or video comes from a script in the repo, with sample counts. Anything not run is labelled `NOT_RUN`.

## 3. What we took from each plan

| Source | Adopted | Not adopted (why) |
|---|---|---|
| Hybrid Intelligent Alert Triage (ours) | RabbitMQ, React, **weighted score** (P1, explainable, sorts the feed and picks the lane, *never overrides an explicit rule*), load-test focus | Spring Boot → **FastAPI** (empty repo; faster to iterate and review). **Redis → none** (no measured bottleneck; Postgres holds state) |
| Intelligent Alert Funnel / six layers (GPT) | L1–L6 vocabulary (§5), **rule impact simulator** (= task 2.4 "Propose fixes"), **critical lane** (2 worker pools), **funnel dashboard**, decision timeline | Full causal root-cause graph → P2 "related alerts" only (time window + static dependency map; no causal claims) |
| One-day MVP (Spanish) | Contract first, vertical slice first, mock API fixtures for the frontend, deterministic core, verify AI-written code | Its scope misses explicit requirements (unknown schema, runbook actions/approval, 4 channels, preferences, live push, hot reload) |
| GPT final reviewer pass | Stricter event identity (§CONTRACT 2), schema `needs_review` with safe fallback, independent golden tests, conservative deadline | — |
| Claude review v2 | Rules on raw paths + `$aliases`, `group.by` alert keys (no mask-all-numbers fingerprint), runbook action registry + approvals, SSE, hot reload, gates | Deadman rules, per-tenant token buckets → CUT for tonight |

## 4. Scope

**P0 — must work by G2 (covers every explicit requirement)**
- 2.1: three sources: RabbitMQ (workflow + trade envelopes), HTTP (Alertmanager + generic), tailed log file. Arbitrary JSON is flattened to paths. Schema registry with `$aliases`. Unknown schema → `needs_review`.
- 2.2: rule DSL (any field, `all/any/not`, eq/neq/in/gt/gte/lt/lte/glob/regex/contains/exists). Per-rule route + presentation (title/body templates, fields, colour). Runtime changes via API, UI and watched config files.
- 2.3: email (Mailpit, real SMTP), SMS + phone (fake-telecom), UI. Per-user/team preferences with time windows, on-call. Outbox with retries, backoff and circuit breaker, so no loss when a channel is down.
- 2.4: live feed over SSE, ack/resolve, rule and preference management in the UI, per-rule look.
- 2.5: dedup counter (1 alert × N occurrences), `immediate` + `digest` grouping, per-recipient throttle.
- 2.6: runbooks in YAML steps / Markdown / free text / URL, variable fill-in, per-channel trimming. Action registry (http_get, retry_workflow, restart_service, call_webhook, open_ticket, escalate). Approval for risky steps in the UI, audit log, auto-close (`resolve_when`, action `resolves: true`).
- Evidence: golden runner, load test (throughput, e2e p50/p99, backlog), README with "what is mocked".

**P0.5 — LLM onboarding assist (directly answers "judges bring an unseen schema/runbook")**
- `assist.suggest_schema(sample)`: detect + aliases + severity map + event_id/alert_key templates (DATA).
- `assist.structure_runbook(text)`: steps mapped onto the registered action list, with approval flags (DELIVERY).
- Each has a deterministic fallback (name-similarity aliases; Markdown numbered list → manual checklist).

**P1 — differentiators, in order**
1. Rule impact simulator / Propose fixes: replay the last N events through the same pure engine with old vs draft rules and show the diff (RULES + UI).
2. NL → rule draft with the LLM, then validate, preview and save (RULES + UI).
3. Reply-to-approve over SMS (`YES 4821`) (DELIVERY).
4. Critical lane: separate worker pool + delivery priority (RULES + DELIVERY).
5. Weighted score with an explanation, used for feed sort and lane (RULES).
6. Funnel dashboard: raw → accepted → grouped → notified → critical, plus throughput, backlog and retries (DATA + UI).
7. Channel fallback after the circuit opens, e.g. SMS down → email (DELIVERY).
8. LLM "suggest a fix" for noisy alerts, and LLM runbook update from the audit trail of a resolved alert (2.6 optional) (RULES / DELIVERY).

**P2:** related-alerts panel (time window + `config/dependencies.yaml`), LLM alert summary, threshold rule type (`count ≥ N in window`), deadman rule.
**CUT:** real SMS/phone providers, SSO/RBAC beyond API keys and approver lists, Kafka + RabbitMQ both, Redis, K8s, multi-region, ML root cause.

## 5. Architecture

```
 L1 Ingest                L2 Validate/Normalise        L3+L4 Decide                     L5 Deliver                L6 Act
 ─────────                ─────────────────────        ────────────                     ──────────                ──────
 RabbitMQ consumer ─┐                                  rule-worker (lane=critical|normal)  delivery-worker/channel   API: alerts, runbooks,
 HTTP /v1/events  ──┼─► flatten + schema detect ─► events (INBOX) ─► pure engine ─► alerts ─► notifications (OUTBOX) ─► Mailpit / fake-telecom / UI
 log tailer ────────┘    + aliases + identity        SKIP LOCKED     group/count/score  + routing.plan()    retries, breaker   actions → mock-ops
                         (commit, then ACK)                          (same transaction) (pure, by DELIVERY)  attempts table     approvals, audit
                                                                                                                                │
 Postgres LISTEN/NOTIFY ──► API SSE /v1/stream ──► React UI (feed, alert detail, rules + preview, onboarding, preferences, funnel)
 config/*.yaml ──watch──► config_objects (versioned) ──NOTIFY config_changed──► workers reload
 assist (Claude API, optional) ◄── onboarding / runbook / rule-draft endpoints only
```

Processes: `api` (FastAPI, scales horizontally), `ingest-amqp`, `ingest-logtail`, `rule-worker` ×N (one or more with `--lane critical`), `delivery-worker` ×N, `scheduler` (digests, escalation timers), plus infra `postgres`, `rabbitmq`, `mailpit` and mocks `fake-telecom` and `mock-ops` (network alias `counterparty-service`).

## 6. Team & ownership (roughly equal load; everyone ships one LLM feature)

| Role | Owner | Branch | Owns (directories) | Layer |
|---|---|---|---|---|
| **DATA** — Data & Platform | *suggested: Yi* | `data/platform` | `backend/alerting/ingest/`, `schemas/`, `config/` loader, `api/routes_ingest.py`, `routes_schemas.py`, `routes_ops.py`, `routes_stream.py`, `mocks/fake_telecom/`, `loadtest/`, `docker-compose.yml`, `db/migrations/0xx` | L1, L2, SSE, infra, measurement |
| **RULES** — Backend: rules & alert engine ("algorithm") | teammate | `rules/engine` | `backend/alerting/rules/`, `api/routes_rules.py`, `routes_alerts.py`, `db/migrations/1xx` | L3, L4, simulator |
| **DELIVERY** — Backend: routing, delivery & runbooks ("API") | teammate | `delivery/runbooks` | `backend/alerting/routing/`, `delivery/`, `runbooks/`, `api/routes_runbooks.py`, `routes_teams.py`, `routes_notifications.py`, `mocks/mock_ops/`, `db/migrations/2xx` | L5, L6 backend |
| **UI** — Frontend | teammate | `ui/frontend` | `frontend/` | L6 UI, dashboard |

**Shared files (change only via a PR that mentions everyone):** `docs/CONTRACT.md`, `backend/alerting/contracts.py`, `backend/alerting/llm.py`, `backend/alerting/db.py`, `backend/alerting/clock.py`, `backend/alerting/api/app.py`, `db/migrations/001_init.sql`, `config/*.yaml` formats.
Yi does the final acceptance (golden + private holdout + chaos) on top of the DATA role.

## 7. Working agreement

- **Branches:** each person works on their own branch, cut from `main`. Rebase on `main` at least every 2 h. Open a PR into `main` at every gate (smaller PRs welcome). Yi merges.
- **Contract changes:** never silently. Edit `CONTRACT.md` and `contracts.py` in a tiny PR first, post in the team chat, then implement.
- **Migrations:** numbered ranges avoid conflicts: `0xx` DATA, `1xx` RULES, `2xx` DELIVERY. Only add files; never edit a merged migration.
- **AI agents:** one coding agent per person, working only inside that person's directories. Give it your handoff file as the task. Tests must pass before you push. Agent-written code isn't "done" until you've run it.
- **Status words:** PASS / FAIL / NOT_RUN in `docs/GATES.md`. Don't write "should work".
- **Secrets:** `ANTHROPIC_API_KEY` lives only in your local `.env` (git-ignored). Only synthetic data. Redact before any LLM call (see CONTRACT §11).

## 8. Gates (Fri 20:30 → Sat)

The deadline is unconfirmed (one plan says Sat 23:00; the 18-hour format suggests ~11:00–12:00). **Plan for a safe submission by 10:30.** If the deadline turns out to be 23:00, the morning build becomes the fallback and we polish until 21:00.

| Gate | When | Must be PASS to proceed |
|---|---|---|
| G0 contract | Fri 21:00 | Everyone has read BRIEF + CONTRACT + own handoff. `make infra` runs on every laptop. Open questions asked to a mentor |
| G1 vertical slice | Sat 00:00 | RabbitMQ `workflow_failed.json` → inbox → rule → alert → outbox → fake-telecom shows phone + SMS → UI card appears live (SSE). Merged to `main` |
| G2 all four example rules | Sat 04:00 | Golden cases for Settlements, Rates, Anna, Pricing, dedup, storm-500, unknown-schema, approval all PASS. Three runbook formats render |
| G3 feature freeze | Sat 07:30 | P0.5 + top P1 items. Holdout run by Yi. Chaos (channel down) reconciles to 0 lost. Load-test raw results committed. Fresh-clone timing measured |
| G4 safe submission | Sat 10:30 | README, video uploaded to Lockbox `All Teams/<team>` + repo link text file |

If G2 slips: cut P1 items, never P0. Don't move G3.

## 9. Demo storyboard (5:00)

| t | Shot |
|---|---|
| 0:00 | The problem in one sentence: the right person finds out late, or drowns in 500 duplicates |
| 0:20 | Three sources at once → Settlements gets phone + SMS (fake-telecom phone UI), Rates gets an email (Mailpit), Anna's SMS vs email depends on the clock |
| 1:10 | Storm: 500 failures → 1 red card counting up live. Funnel dashboard: 500 → 1 → 2 sends |
| 1:40 | Runbook: steps already filled with the workflowId → retry → fails twice → escalate needs approval → lead replies `YES 4821` by SMS → audit trail → auto-resolve |
| 2:40 | Unseen schema live: paste the judge's JSON → `needs_review` → LLM suggests the mapping → confirm → create a rule from the sample → next event routes. Unseen runbook pasted → structured steps |
| 3:30 | "This alert is too noisy" → proposed rule (LLM) → impact preview (−80 % sends, 0 critical alerts lost on the replayed sample) → approve → hot reload |
| 4:00 | Measured: msgs/s ingested and processed, e2e p50/p99 with N, 1 vs 2 workers, channel-down chaos = 0 lost |
| 4:40 | One file to add a provider or action. Fresh clone → N commands → first alert in M min. What's mocked. How we used AI and handled privacy |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Integration at the end | G1 vertical slice by midnight. UI builds against `fixtures/api/` mocks until then |
| Two people editing shared files | Ownership table + migration ranges + contract-change PRs |
| Hidden judge inputs | Generic flatten + `needs_review` + LLM assist + Yi's private holdout at G3 |
| LLM latency or outage during the demo | Assist is never on the alert path. 20 s timeout. Deterministic fallback. Cache suggestions per schema fingerprint |
| Deadline ambiguity | Safe submission at 10:30 regardless |
| A storm on one alert row | Batch claim + in-batch pre-aggregation (`count = count + k` once per key per batch) |

## 11. Ask a mentor (at G0)

1. Exact submission deadline and Lockbox path.
2. 2.4 "Propose fixes / enhancements to the alerts": is it people proposing changes, or the system proposing fixes? (We plan both: a human proposal + LLM suggestion, then impact preview, then owner approval.)
3. Will the judge's new schema arrive over HTTP, the broker or a file? (We support all three; the generic sources accept anything.)
