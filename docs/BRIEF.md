# Alert Funnel — Team Brief

GS Warsaw Hackathon 2026 · "Flexible Alerting & Notification Service"
Version: Fri 2026-11-20 ~21:15 CET · Tech lead: Yi

**Read order (15 min):** this brief → [AGENTS.md](../AGENTS.md) → [CONTRACT.md](CONTRACT.md) → your handoff:

| Who | Role | Branch | Handoff |
|---|---|---|---|
| **Yi** (GitHub `yy31104`) | DEV1 · Tech lead + backend core | `feat/backend-yi` | [DEV1-backend-Yi.md](handoff/DEV1-backend-Yi.md) |
| **daizhongtian** (GitHub `daizhongtian`) | DEV2 · Data & platform | `feat/data-daizhongtian` | [DEV2-data-daizhongtian.md](handoff/DEV2-data-daizhongtian.md) |
| **zhaoqinghe** (GitHub `Petitaire`) | DEV3 · Frontend | `feat/frontend-zhaoqinghe` | [DEV3-frontend-zhaoqinghe.md](handoff/DEV3-frontend-zhaoqinghe.md) |
| **Marlon Mejia** (GitHub `MarlonMejia15`) | DEV4 · Review & presentation (joins at G3) | `feat/review-marlon` | [DEV4-review-Marlon.md](handoff/DEV4-review-Marlon.md) |

---

## 1. What we build

A service that ingests events from many sources and formats (RabbitMQ, HTTP webhooks, a tailed log file), lets each team write rules on **any field**, and delivers **the right alert to the right people on the channel they prefer** (email / SMS / phone / live UI). It doesn't flood them: duplicates are collapsed, real occurrences are counted, and sends are grouped and throttled. Then it helps them **resolve** the alert with runbooks whose steps can be executed, with human approval for risky steps and an audit trail.

**Events are stored as-is and stay queryable.** Every event keeps its raw JSON plus a flattened, indexed `path → value` map. Known formats are *configured* in a schema registry with aliases like `$severity` and `$service`, not hard-coded. Unknown formats are kept and flagged `needs_review`, so a developer or an agent can always query them. The judges will bring an unseen schema and an unseen runbook to the demo. We onboard both without code: an LLM suggests, a human confirms.

**First format to build with:** the workflow-failure event from Appendix A (`fixtures/events/workflow_failed.json`) over RabbitMQ. Next: the Alertmanager webhook over HTTP, the `meta/payload` business event over RabbitMQ, and the pricing log file.

**In one line:** *Prevent* (verify sources and rules before they go live) · *Detect* (reliable ingest, rules, grouping, delivery) · *Resolve* (runbooks, approvals, audit), and stay correct under bad input, traffic storms and channel failures.

## 1b. What it looks like when it's done

A judge, or a team that's new to the system, can:
1. **Send events in different shapes** over RabbitMQ, HTTP or a log file. Each one is stored, recognised (or flagged "needs review"), and queryable by any field.
2. **Write a rule on any field** in the UI (AND/OR, any path), see it as a plain-English sentence, and test it against a real event. It takes effect without a restart.
3. **Watch an alert appear live** with no page refresh. It is coloured by its rule and has a counter: 500 identical failures = 1 alert × 500, and the on-call person is paged once.
4. **See the right people notified on the channel they prefer:**
   - a fake phone call + SMS, a Mailpit email, and the UI;
   - Anna's preference honoured: SMS during the day, email at night;
   - every attempt and retry visible;
   - the SMS gateway taken down without losing anything.
5. **Handle the alert to the end.** Open it and read "why was I notified". Follow the runbook with the real values filled in. Run a step, request approval for a risky one, and approve it (in the UI or by replying to the SMS). Watch the alert close itself once the problem goes away. All of this lands in the audit trail.
6. **Paste an event format nobody has seen.** The system shows what's missing, the LLM proposes a mapping, a person confirms, and the next event is routed, with no code change. The same works for a runbook in an unseen format.
7. **Propose a fix** on a noisy alert. As P1: preview its impact before the owner approves it.
8. **Read measured numbers:** events/s, end-to-end p50/p99 with N, and the reconciliation after a channel outage.

## 2. Non-negotiables

1. **Durable before ack.** An event is committed to the `events` inbox before HTTP returns 202 or RabbitMQ gets its ACK. A notification is committed to the `notifications` outbox in the same transaction as the incident change.
2. **Three identities, never mixed.**
   - *Event identity:* a retransmission is not a new fact.
   - *Alert key:* 500 real failures = 1 incident with count 500.
   - *Notification id:* 3 send attempts = 1 notification.
3. **Deterministic core.** Matching, routing and delivery never wait for an LLM. The LLM only suggests (schema mapping, runbook structure, rule drafts). A human confirms, and a fallback always exists.
4. **Nothing silently dropped.**
   - Unknown schema → `needs_review`, routed to the fallback team.
   - Missing template field → shown as `‹missing: path›`.
   - A critical alert always reaches at least one channel.
5. **Measured, not estimated.** Numbers come from scripts in the repo, with sample counts. Anything not run is marked `NOT_RUN`.

## 3. Who does what

| | Yi · DEV1 · backend core | daizhongtian · DEV2 · data & platform | zhaoqinghe · DEV3 · frontend |
|---|---|---|---|
| Layers | L3 grouping, L4 decision, L5 routing + delivery, L6 runbook actions | L1 ingest, L2 normalise, config, LLM assist, live stream, infra, measurement | L6 user experience, dashboard |
| Core | Rule DSL + **pure** engine, rule-worker (grouping, counting, P0–P3 priority), incidents API, pure `routing.plan` (preferences, time windows, on-call), delivery worker (retries, breaker), providers, runbook render + actions + approvals + audit | HTTP / RabbitMQ / log-file ingest, normalise + schema registry + dedup, unknown schemas + event query API, config store + hot reload + all config CRUD APIs, LLM assists, rule simulator, SSE, fake-telecom + mock-ops, compose + Dockerfile, golden runner + load/chaos | Live feed (SSE), incident detail + timeline + explain, runbook panel + approvals, rule editor + impact preview, preferences, onboarding page for unknown formats, funnel dashboard |
| Also | Tech lead: shared files, CI, reviews and merges PRs into `main` | | Starts on mock data in `fixtures/api/`, switches to the real API screen by screen |

**Marlon (DEV4)** joins at G3 (~07:30) with fresh eyes: product review, the private hidden-input rehearsal, an evidence audit, the README for judges, the ≤5-min video and Q&A prep.

**The seams (who calls whom):**
```
daizhongtian: ingest ──writes──► events (INBOX: raw + flattened fields)
Yi: rule-worker ──claims events──► engine.evaluate (pure)
            ──calls──► routing.plan (pure) ──► incident + notifications (OUTBOX)   [one transaction]
Yi: delivery-worker ──claims outbox──► Mailpit / fake-telecom / UI
daizhongtian: SSE ◄── NOTIFY from Yi's workers ──► zhaoqinghe's UI
daizhongtian: simulator + rule validation ──call──► Yi's pure engine / dsl / routing.plan (no side effects)
zhaoqinghe: UI ──HTTP──► /v1/* (owners listed in CONTRACT §9)
```
Write **pure functions with tests first** (fixtures in `fixtures/`), then wire DB, queues and HTTP around them.

## 4. Scope

**P0 — must work by G2. Covers every explicit requirement in 2.1–2.6:**
- **2.1** ≥ 2 source types plus the log file, arbitrary schemas, a new schema is config not code (daizhongtian).
- **2.2** Rules on any field with AND/OR, per-rule routing and presentation (Yi). Runtime changes via API / UI / config files (daizhongtian).
- **2.3** Email / SMS / phone / UI, preferences with working hours, on-call, simulated providers behind one interface, retries, no loss when a channel is down (Yi; fake-telecom by daizhongtian).
- **2.4** Live push feed, ack/resolve, manage rules and preferences, per-rule look (zhaoqinghe on Yi's and daizhongtian's APIs), plus a minimal **Propose fix** on any alert: the text and author are saved (daizhongtian API, zhaoqinghe button).
- **2.5** One alert with a counter instead of 500, grouping and throttling (Yi).
- **2.6** Runbooks in YAML / Markdown / text / link, values filled in, shown per channel, actions with approval, audit, auto-close (Yi). Runbook store (daizhongtian).
- **Evidence:** golden runner and load test (throughput, e2e p50/p99 with N, backlog) (daizhongtian), plus three experiments with the same input before and after:
  1. **Poison event:** the bad event is traceable and the rest complete.
  2. **Hot-key storm vs wide variety, with critical events injected throughout:** critical p50/p99, failures, oldest pending, decisions and SMS per 1,000 events.
  3. **Provider outage:** outbox reconciliation with lost = 0.

  README "what is mocked" (Marlon).

**P0.5 — LLM onboarding (daizhongtian):** schema-mapping suggestion and runbook structuring, each with a deterministic fallback.

**P0 hardening — reliability under bad input and failures (Yi, before any new feature):**
1. **Poison-event isolation.** One bad event never blocks the rest. Errors are classified: deterministic bad input → quarantine that event with its reason; transient DB errors → roll back and retry; a faulty rule → skip that rule, record it, keep processing everything else. Each event has a retry budget.
2. **Approval security.** 6-digit codes valid for 5 minutes, a persistent failed-attempt limit with audit, and the inbound SMS callback authenticated with a shared secret (the `from` field alone can be spoofed). `X-User` stays demo-only.
3. **Database timeouts.** Statement, lock and idle-in-transaction timeouts sized to the batch; a timeout rolls back and retries safely.
4. **A stale recovery never closes a newer incident.** A resolve event only closes the alert if it belongs to the alert's latest episode (`episode_key`); otherwise it is kept as evidence.
5. **A runbook action that times out is "unknown", not "failed".** Check the receiver with the same execution id before anyone re-runs it.

**P1 — innovations, in order:**
1. **Sample-based source verification (headline).** When a source or schema is onboarded, its owner adds one "should notify" and one "should stay quiet" sample. The preview runs both through the real engine and routing and shows recipients, channels and **the field that decided it**. Numeric unit/scale transforms are configured (e.g. a notional sent in cents). A missing unit that would change the decision is flagged instead of guessed. (daizhongtian + zhaoqinghe; engine by Yi)
2. **Rule Safety Checker.** Before a rule is published, it generates a finite set of scenarios: severity × environment × time-of-day boundaries × missing contact. It runs the real engine + routing and shows counterexamples such as "prod critical at 23:10 reaches nobody". It reports which scenarios were checked and never claims "proved safe". (Yi `rules/safety.py`, daizhongtian endpoint, zhaoqinghe panel)
3. Rule impact preview + AI suggestions for proposals (daizhongtian + zhaoqinghe).
4. **Delivery-risk metrics:** oldest pending notification per channel/lane vs a target, backlog, retries; shown as an "at risk" badge (daizhongtian + zhaoqinghe).
5. **SSE coalescing:** counter updates at most every ~400 ms per alert. Created / critical / approval / resolved events go out immediately, and a reconnect refetches state (daizhongtian).
6. **Schema-drift flag:** a known source suddenly lacks fields its rules depend on (daizhongtian).
7. **Recovery hysteresis**, only with positive evidence (`stable_for`) (Yi).
8. Already built, keep tested: reply-to-approve by SMS, critical lane + weighted score, channel fallback (Yi). Funnel dashboard and natural-language rule draft (daizhongtian + zhaoqinghe).

**P2 — only after the whole chain works:**
- **Incident Investigator:** read-only, async after the incident is created, at most 3 calls to allow-listed and clearly simulated mock-ops endpoints. It never touches the action registry, and it shows tool, time, evidence and unknowns.
- Shadow rules.
- SLO / error-budget priority (needs request totals).
- Incident memory cards.
- Cross-rule notification merging.
- End-to-end canary.
- Dynamic escalation on rate spikes.
- Cost ladder (never overriding a rule's explicit channels).
- Related incidents.
- Threshold and deadman rules.

**Not doing without a measured bottleneck:** COPY staging tables, index restructuring, deleting history, Redis.

**CUT:** real SMS/phone providers, SSO/RBAC, Kafka, Redis, K8s, multi-region, ML root cause.

## 5. GitHub workflow

- `main` is the stable branch. **Nobody pushes to `main`**; changes reach it only through PRs.
- One branch per person: `feat/backend-yi`, `feat/data-daizhongtian`, `feat/frontend-zhaoqinghe`, `feat/review-marlon`. All are cut from `main`, so every branch already contains the brief, contract, AGENTS.md, config, fixtures and every handoff.
- Open a small PR into `main` every 1–2 hours. CI (`.github/workflows/ci.yml`) must be green. Yi reviews and merges.
- After each merge, everyone runs `git pull --rebase origin main` on their own branch.
- **Shared files** change only through a small PR from Yi after telling the team: `docs/CONTRACT.md`, `AGENTS.md`, `backend/alerting/contracts.py`, `llm.py`, `templates.py`, `clock.py`, `db.py`, `api/app.py`, `db/migrations/001_init.sql`, `.github/`.
- **Migrations:** only add files, in your own range: `1xx` Yi, `2xx` daizhongtian. Never edit a migration that's already merged.
- **AI agents:** one coding agent per person, on that person's branch, working inside that person's directories. Every agent reads `AGENTS.md` first. A feature counts as done only when it runs from the integrated `main`.
- Never commit `.env` or keys. Use synthetic data only.

```bash
git clone https://github.com/yy31104/warsaw-hackathon-2026.git && cd warsaw-hackathon-2026
git switch feat/data-daizhongtian                  # your branch (already exists)
cp .env.example .env && make infra && (cd backend && uv sync) && make test
git push origin feat/data-daizhongtian             # then open a PR into main
```

## 6. Gates

The deadline isn't confirmed: either ~11:00 Sat (18-hour format) or 23:00 Sat. **We plan a safe submission at 10:30.** If the deadline turns out to be 23:00, that morning build becomes the fallback and we polish until 21:00.

| Gate | When | Must PASS |
|---|---|---|
| G0 base | Fri 21:30 | Everyone has cloned, read their handoff, `make infra` + `make test` green. Mentor asked: deadline, Lockbox path, meaning of 2.4 |
| G1 first slice | Sat 00:00 | `workflow_failed.json` over RabbitMQ → inbox → rule → incident → outbox → fake-telecom shows phone + SMS to sam → card appears live in the UI. Merged into `main` |
| G2 all example rules | Sat 04:00 | Golden cases PASS: Settlements, Rates, Anna, Pricing, dedup, storm-500, unknown schema, approvals. Three runbook formats render |
| G3 freeze | Sat 07:30 | P0.5 + top P1 done. Marlon runs the holdout. Channel-down chaos loses 0. Load results committed. Fresh-clone timing done |
| G4 submit | Sat 10:30 | Judge README + ≤ 5-min video + repo-link text file in Lockbox `All Teams/<team>` |

Every 60–90 min, each person posts in the chat: what works (a command or test), what got merged, what they need from whom, what's blocked, what's next.

## 7. Demo storyboard (5:00)

| t | Shot |
|---|---|
| 0:00 | The problem: the right person finds out late, or drowns in duplicates |
| 0:20 | Three sources at once → Settlements gets phone + SMS, Rates gets an email, Anna gets SMS or email depending on the time |
| 1:10 | Storm: 500 failures → 1 red incident counting live. Funnel 500 → 1 → 2 sends |
| 1:40 | Runbook with the real workflowId → retry → fails twice → escalation needs approval → lead replies `YES 4821` by SMS → audit trail → auto-resolve |
| 2:40 | Judge's unseen schema → `needs_review` → LLM-suggested mapping → confirm → rule from the sample → next event routed. Unseen runbook → structured steps |
| 3:30 | "Too noisy" → proposed rule → impact preview → approve → hot reload |
| 4:00 | Measured: msgs/s, e2e p50/p99 (with N), 1 vs 2 workers, channel down = 0 lost |
| 4:40 | One file adds a provider or an action. Fresh clone → N commands. What's mocked. AI use and privacy |

## 8. Risks

| Risk | Mitigation |
|---|---|
| Late integration | G1 slice by midnight. Pure functions + fixtures first. PRs every 1–2 h |
| Agents editing shared files | AGENTS.md + directory ownership + migration ranges + Yi merges |
| Hidden judge inputs | Raw + flattened storage, `needs_review`, LLM assist, Marlon's private holdout at G3 |
| Yi and daizhongtian carry the heaviest load | P1 is cut first. Marlon takes polish, README and video from G3 |
| A storm hammering one incident row | Batch claim + in-batch pre-aggregation (`count = count + k`) |
| Deadline ambiguity | Safe submission at 10:30 regardless |
