# Alert Funnel — Team Brief (final split)

GS Warsaw Hackathon 2026 · "Flexible Alerting & Notification Service"
Version: Fri 2026-11-20 ~20:45 CET · Tech lead: Yi (DEV1)

**Read order (15 min):** this brief → [CONTRACT.md](CONTRACT.md) → your handoff:
[DEV1 Platform](handoff/DEV1-platform.md) · [DEV2 Ingestion](handoff/DEV2-ingestion.md) · [DEV3 Incidents](handoff/DEV3-incidents.md) · [DEV4 Notifications](handoff/DEV4-notifications.md) · [UI](handoff/UI.md) · [Review & present](handoff/REVIEW.md)

---

## 1. What we build

A service that ingests events from many sources and formats (RabbitMQ, HTTP webhooks, a tailed log file), lets each team write rules on **any field**, and delivers **the right alert to the right people on the channel they prefer** (email / SMS / phone / live UI). It doesn't flood them: duplicates are collapsed, real occurrences are counted, and sends are grouped and throttled. It then helps them **resolve** the alert with runbooks whose steps can be executed, with human approval for risky steps and an audit trail.

**Events are stored as-is and stay queryable.** Every event keeps its raw JSON and a flattened `path → value` map (indexed). Known formats are *configured* (schema registry: aliases like `$severity`, `$service`), not hard-coded. Unknown formats are kept and flagged `needs_review`, so a developer or an agent can always query and investigate them. The judges will bring an unseen schema and an unseen runbook. We onboard both without code, with an LLM suggesting and a human confirming.

**First format to build with:** the **workflow-failure** event from Appendix A (`fixtures/events/workflow_failed.json`) over RabbitMQ. It's the simplest flat JSON. The other Appendix A formats (Alertmanager webhook over HTTP, `meta/payload` business event over RabbitMQ) and the pricing log file follow once the slice works.

## 2. Non-negotiables

1. **Durable before ack.** An event is committed to the `events` inbox before HTTP returns 202 or RabbitMQ gets its ACK. A notification is committed to the `notifications` outbox in the same transaction as the incident change.
2. **Three identities, never mixed.** *Event identity* (a retransmission is not a new fact) ≠ *alert key* (500 real failures = 1 incident, count 500) ≠ *notification id* (3 send attempts = 1 notification).
3. **Deterministic core.** Matching, routing and delivery never wait for an LLM. The LLM only suggests (schema mapping, runbook structure, rule drafts), a human confirms, and there is always a fallback.
4. **Nothing silently dropped.** Unknown schema → `needs_review` + fallback team. Missing template field → `‹missing: path›`. A critical alert always reaches at least one channel.
5. **Measured, not estimated.** Numbers in the README or video come from scripts in the repo, with sample counts. Anything unrun is `NOT_RUN`.

## 3. The four roles (from our team plan, mapped onto the full task)

| | Role | Owner | Branch | Owns |
|---|---|---|---|---|
| **DEV1** | Tech lead: platform, DB, integration, evidence | **Yi** | `feat/platform` | Shared contracts, DB schema & migrations, Docker Compose, Dockerfile, CI, AGENTS.md, PR reviews & merges, mock-ops, golden runner, load/chaos report, `/health` `/metrics` `/stats`. Second track: the **UI** until someone frees up (see §5) |
| **DEV2** | Ingestion & data: HTTP, RabbitMQ, normalisation, dedup, config | teammate | `feat/ingestion` | Sources (HTTP, RabbitMQ, log tail), flatten + schema registry + aliases + severity, event identity (dedup), unknown-schema onboarding, event query API, config store + hot reload + config CRUD APIs, LLM assists (schema suggestion, runbook structuring), load generator. P1: rule simulator, NL rule draft |
| **DEV3** | Incidents & priorities: grouping, rules, criticality, resolution | teammate | `feat/processing` | Rule DSL + pure engine, rule-worker (grouping, counting, P0–P3 priority, score, lanes), digest/cooldown scheduler, incidents API (list/detail/ack/resolve, explain), runbook **actions + approvals + audit + auto-resolve** |
| **DEV4** | Notifications & routing: recipients, outbox, delivery, retries | teammate | `feat/delivery` | Directory (teams, on-call, preferences with time windows), pure `routing.plan`, runbook matching + rendering per channel, providers (Mailpit email, fake-telecom SMS/phone, UI), delivery worker (retries, breaker, fallback), escalation, reply-to-approve, **SSE live stream** |

How the team plan's words map to the code: *ingestion* = `alerting/ingest`, `schemas`, `config`; *processing / incidents* = `alerting/rules` + `runbooks/actions`; *routing / delivery* = `alerting/routing`, `delivery`, `runbooks/render`. "Incident" in our talk = `alerts` table / `/v1/alerts` API (the task text says "alert").

**The seams (who calls whom):**
```
DEV2 ingest ──writes──► events (INBOX, raw + flattened fields)
DEV3 rule-worker ──claims events──► engine.evaluate (DEV3, pure)
                 ──calls──► routing.plan (DEV4, pure) ──► incident + notifications (OUTBOX)  [same transaction]
DEV4 delivery-worker ──claims outbox──► Mailpit / fake-telecom / UI (SSE)
DEV3 actions ──may enqueue──► approval_request / escalation notifications (via routing.plan)
DEV2 simulator + rule validation ──call──► engine.evaluate / dsl.compile / routing.plan (pure, no side effects)
DEV1 ──owns──► contracts.py, 001_init.sql, compose, CI; merges everything
```
Everyone writes **pure functions with tests first** (fixtures in `fixtures/`), then wires DB, queues and HTTP around them.

## 4. Scope

**P0: must work by G2. Every explicit requirement in sections 2.1–2.6.**
- 2.1 ≥ 2 source types (RabbitMQ + HTTP; the log file covers the Pricing rule), arbitrary schemas, new schema = config not code (DEV2).
- 2.2 rules on any field, AND/OR, per-rule routing + presentation, runtime changes via API/UI/config files (DEV3 engine, DEV2 config APIs).
- 2.3 email/SMS/phone/UI, per-user/team preferences with working hours, on-call, simulated providers behind one interface, retries + no loss when a channel is down (DEV4).
- 2.4 live feed by push, ack/resolve, manage rules and preferences, per-rule look (UI + DEV3/DEV4/DEV2 APIs).
- 2.5 one alert with a counter for 500 duplicates, grouping/throttling per rule or recipient (DEV3, DEV4).
- 2.6 runbooks in YAML / Markdown / free text / link, values filled in, shown per channel; actions with approval for risky ones; audit; auto-close (DEV4 render, DEV3 actions).
- Evidence: golden runner, load test (throughput, e2e p50/p99 with N, backlog), README with "what is mocked" (DEV1).

**P0.5: LLM onboarding, answers "judges bring an unseen schema/runbook" (DEV2):** schema-mapping suggestion and runbook structuring, each with a deterministic fallback.

**P1, in order:** (1) rule impact preview / "Propose fixes" (DEV2 + UI), (2) reply-to-approve by SMS (DEV4), (3) critical lane + weighted score (DEV3), (4) funnel dashboard (DEV1 + UI), (5) channel fallback (DEV4), (6) NL → rule draft (DEV2).
**P2:** related-incidents panel, LLM incident summary, threshold and deadman rule types.
**CUT:** real SMS/phone providers, SSO/RBAC, Kafka, Redis, K8s, multi-region, ML root cause.

## 5. Open gap: the frontend

Our team plan covers the backend. The task **requires** a UI (live push feed, ack/resolve, rule and preference management), and the UX is judged directly. The video is made of UI screens. **Decision:** DEV1 starts the UI with a coding agent right after G0, against the mock fixtures in `fixtures/api/` (the base is already laid, so DEV1 has capacity). The first developer to finish their G2 items joins the UI. The late-joining teammate takes [REVIEW.md](handoff/REVIEW.md) (fresh-eyes review + video) from G3.

## 6. GitHub workflow

- `main` = stable and protected by agreement: **nobody pushes to `main` directly**. One branch per developer (`feat/platform`, `feat/ingestion`, `feat/processing`, `feat/delivery`, `feat/frontend`).
- Small PRs into `main` at least every 1–2 hours. CI (`.github/workflows/ci.yml`) must be green. DEV1 reviews and merges. After a merge, everyone runs `git switch <your-branch> && git pull --rebase origin main`.
- **Shared files** (`docs/CONTRACT.md`, `backend/alerting/contracts.py`, `llm.py`, `templates.py`, `clock.py`, `db.py`, `api/app.py`, `db/migrations/001_init.sql`, `docker-compose.yml`): change only via a small PR from DEV1 after telling the team.
- **Migrations:** add files only, in your range: `0xx` DEV1, `1xx` DEV2, `2xx` DEV3, `3xx` DEV4. Never edit a merged migration.
- **AI agents:** one agent per developer, on that developer's branch, inside that developer's directories. Every agent reads `AGENTS.md` first. A feature isn't done until it runs from the integrated `main`.
- Never commit `.env` or real keys. Synthetic data only.

```bash
git clone https://github.com/yy31104/warsaw-hackathon-2026.git && cd warsaw-hackathon-2026
git switch feat/ingestion            # your branch (already created)
cp .env.example .env && make infra && (cd backend && uv sync) && make test
# work, commit small
git push origin feat/ingestion       # then open a PR into main
```

## 7. Gates

The deadline is unconfirmed: ~11:00 Sat (18-hour format) or 23:00 Sat (team plan). **Plan for a safe submission at 10:30.** If it turns out to be 23:00, the morning build is the fallback and we polish until 21:00.

| Gate | When | Must PASS |
|---|---|---|
| G0 base | Fri 21:15 | Everyone has cloned, read their handoff, and has `make infra` + `make test` green. Mentor asked: deadline, Lockbox path, meaning of 2.4 |
| G1 first slice | Sat 00:00 | `workflow_failed.json` over RabbitMQ → inbox → rule → incident → outbox → fake-telecom shows phone + SMS to sam → card appears live in the UI (SSE). All merged into `main` |
| G2 all example rules | Sat 04:00 | Golden cases for Settlements, Rates, Anna, Pricing, dedup, storm-500, unknown-schema and approvals PASS. All three runbook formats render |
| G3 freeze | Sat 07:30 | P0.5 + top P1. Holdout by DEV1. Channel-down chaos = 0 lost. Load results committed. Fresh-clone timing |
| G4 submit | Sat 10:30 | Judge README + ≤ 5-minute video + repo-link text file in Lockbox `All Teams/<team>` |

Every 60–90 min each developer answers in chat: what works (command/test), what's merged, what I need from whom, what's blocked, what's next.

## 8. Demo storyboard (5:00)
| t | Shot |
|---|---|
| 0:00 | The problem: the right person finds out late, or drowns in duplicates |
| 0:20 | Three sources at once → Settlements gets phone + SMS (fake-telecom page), Rates gets email (Mailpit), Anna's SMS vs email depends on the time |
| 1:10 | Storm: 500 failures → 1 red incident counting live; funnel 500 → 1 → 2 sends |
| 1:40 | Runbook with the real workflowId → retry → fails twice → escalate needs approval → lead replies `YES 4821` by SMS → audit → auto-resolve |
| 2:40 | Judge's unseen schema: paste → `needs_review` → LLM-suggested mapping → confirm → rule from the sample → next event routed. Unseen runbook → structured steps |
| 3:30 | "Too noisy" → proposed rule → impact preview → approve → hot reload |
| 4:00 | Measured: msgs/s, e2e p50/p99 (N), 1 vs 2 workers, channel down = 0 lost |
| 4:40 | One file adds a provider or action; fresh clone → N commands; what's mocked; AI use and privacy |

## 9. Risks
| Risk | Mitigation |
|---|---|
| Late integration | G1 slice by midnight; pure functions + fixtures first; PRs every 1–2 h |
| Agents editing shared files | AGENTS.md rules + owner directories + migration ranges + DEV1 merges |
| Hidden judge inputs | Raw + flattened storage, `needs_review`, LLM assist, DEV1's private holdout at G3 |
| Frontend has no owner | §5: DEV1 starts it on mocks; first free developer joins |
| Hot incident row in a storm | Batch claim + in-batch pre-aggregation (`count = count + k`) |
| Deadline ambiguity | Safe submission at 10:30 regardless |
