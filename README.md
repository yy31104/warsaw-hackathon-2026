# Alert Funnel

**Know what matters. Know what to do.**

Alert Funnel takes events from many systems and formats, decides which ones matter, notifies the right person on the channel they prefer, and guides a safe response. It was built in 18 hours for the Goldman Sachs Warsaw Hackathon 2026 (*Flexible Alerting & Notification Service*).

Everything here runs locally with Docker. Phone, SMS, email and operational actions go to simulators, never to real people or systems.

---

## Try it in three steps

You need Docker Desktop (or Docker Engine with Compose). Run everything from the repository root.

**1. Start the stack** (25 s from a fresh clone with a warm Docker cache; the first build takes longer)

```sh
docker compose up -d --build --wait
```

**2. Open the console:** <http://localhost:5173>

A short intro plays once; press **Skip** at any time. **Tour** (bottom left) walks you through the pages. **Viewing as** (top right) switches between demo people, e.g. Sam (on call for Settlements), Lee (the Settlements backup and approver) and Ops (platform team).

**3. Send a first event**

```sh
curl --fail-with-body -sS http://127.0.0.1:8000/v1/events/generic-webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: demo-generic-key' \
  -H 'X-Demo-Clock: 2026-10-23T11:15:00+02:00' \
  --data-binary @fixtures/events/alertmanager_high_error_rate.json
```

The demo clock puts Anna inside her daytime SMS window, so she gets a text. View the console as **Anna** or **Paul** (Payments) to see the incident, and open the simulated phone at <http://localhost:8100> to see the SMS. To see every decision for the event, call `GET /v1/events/<id>/explain` with the first id from `event_ids`.

| What | Where |
| --- | --- |
| Console | <http://localhost:5173> |
| API | <http://localhost:8000> |
| Simulated phone and SMS | <http://localhost:8100> |
| Email (Mailpit) | <http://localhost:8025> |
| Simulated ops actions | <http://localhost:8200> |
| RabbitMQ admin | <http://localhost:15672> |

**AI assist is off by default.** Everything above works on deterministic rules. To let an LLM draft mappings, rules and runbook steps, copy `.env.example` to `.env`, set `ASSIST_ENABLED=1` and add your own provider key. The LLM is never on the alert path.

---

## Bring your own event or runbook

The judges bring formats we have never seen. This is how to onboard one, with no code change.

**A new event format**

1. Send it as it is:
   ```sh
   curl -sS http://127.0.0.1:8000/v1/events/generic-webhook \
     -H 'Content-Type: application/json' -H 'X-Api-Key: demo-generic-key' \
     --data '{"desk":"rates","amount_minor":25000000,"priority":"P1","trade_ref":"T-90211","timestamp":"2026-11-21 09:30:00"}'
   ```
   It is accepted (HTTP 202) and kept as **needs review**, never dropped.
2. In the console, view as **Ops** and open **Sources**. The event is waiting there.
3. **Suggest mapping** proposes what each field means. Fix anything that's wrong.
4. **Compare possible interpretations** shows which open questions (the unit of `amount_minor`, what `P1` means, the timezone) would change who gets notified, so you answer only those.
5. Add one example that should alert and one that should stay quiet, **Run test**, then **Go live**. The next event in that format is routed by your rules.

Other inputs: HTTP also accepts NDJSON, CSV with a header row, YAML, XML and plain text. RabbitMQ accepts JSON. Log files are tailed with a configured parser; `POST /v1/sources/{id}/parser/suggest` proposes one from a few example lines.

**A new runbook**

1. Open **Runbooks**, choose **New runbook** and paste the instructions as written (Markdown, a checklist, plain text or a link).
2. **Turn into steps** converts them into typed steps. Anything unsafe or unclear (a missing target, a risky step without approval) is flagged and must be fixed before **Publish**.
3. A runbook shows up on incidents from the rules it names. Risky steps wait for a second person to approve, and every run is audited.

---

## What it covers

| The brief asks for | How Alert Funnel does it | See it in |
| --- | --- | --- |
| Many sources and formats | HTTP (6 formats), RabbitMQ, tailed log files. Every event is stored raw plus a flattened field map, so any field is queryable. | Health, Sources |
| Unseen schemas | Unknown events wait in review. A suggested mapping is confirmed by a person and tested with examples before it goes live. | Sources |
| Rules on any field | Conditions on any path with AND/OR, shown as a plain sentence, previewed against past events, hot-reloaded. The last 5 versions can be restored. | Rules |
| Right person, right channel | Email, SMS, phone and the live console. Personal rules by time window and severity, team defaults, and escalation from primary to backup if nobody acknowledges. | On call |
| No flooding | Retransmissions are deduplicated; 500 repeats become one incident with a count of 500; cooldown, digests and throttling. A severity increase or a single critical event still notifies at once. | Now, Incidents |
| Live updates | Server-sent events update the console without a refresh. | Now |
| Resolve with runbooks | Steps with real values filled in, executable actions, approval by a second person, and a full audit trail. | Incident, Runbooks |

---

## Three things we added

**1. Decision stability · ask only what changes the decision** (Sources)
For an unfamiliar event, the system tries each plausible reading of the uncertain fields through the real rules and routing, and asks only the questions whose answers change who gets notified. On 4 held-out synthetic cases it cut the questions per case from 2.75 to 1.0, with routing correct on 4/4 and 0/2 critical misses. [Results](docs/SCORECARD.md#decision-stability-result)

**2. Safe policy optimizer · less noise without losing a required alert** (Rules)
It tries bounded rule changes against stored events and protected cases. In a replay of 18 synthetic night-time events, one option cut planned texts from 18 to 4 with 0 of 14 protected cases violated; another was rejected, with the exact case where it would have dropped a required SMS. It only previews; a person publishes. [Results](docs/SCORECARD.md#innovation-on-final-main)

**3. Unknown outcome reconciliation · never run an action twice by accident** (Incident)
If an action times out, its result is **Unknown** and retry is locked. **Check receipt** asks the receiver about the original execution ID instead of running it again. In the live check the action ran once and a confirmed retry was refused (HTTP 409). [Walkthrough](docs/DEMO_UNKNOWN_OUTCOME.md)

We also borrowed proven patterns from PagerDuty (acknowledge vs resolve, escalation policies, MTTA and MTTR) rather than present them as new.

---

## Measured, not estimated

Every number below comes from a script in this repository and links to its raw output. The scorecard records each target and whether it was set before or after the measurement.

| Measure | Result | Status |
| --- | --- | --- |
| Critical notification p99 at the receiver, 700 normal + 2 critical events/s | **0.356 s**, 240/240 received | PASS (target < 1 s) |
| Normal notification p99, same run | **5.738 s**, 2,000/2,000 received (27.424 s before batching) | **FAIL** (target < 5 s) |
| SMS gateway down for 30 s | **24/24** delivered after recovery, none lost | PASS |
| Rule workers, 1 → 2 → 4 | Backlog starts building at 700 → 1,400 → 2,800 events/s; PostgreSQL CPU is the shared limit | Measured |
| Critical lane under a normal backlog of 56,000 events | Critical p99 **0.443 s**, 240/240 | PASS |
| Backend · frontend · golden · browser tests | 798/798 · 19/19 · 32/32 · 13/13 | PASS |

The normal-lane tail sits before the first delivery attempt. A matched trial showed that larger delivery batches do not fix it; the per-message cost is the next thing to profile. [Scorecard](docs/SCORECARD.md) · [Scaling notes and capacity planning](docs/SCALING.md)

<!-- w0-input-variations-start -->
## How the sample handles input variations

| Change | HTTP | Stored / decision | Observed route |
| --- | --- | --- | --- |
| README baseline | 202 | processed / NEW_ALERT | anna / sms |
| Renamed service field | 202 | processed / UNROUTED | None observed |
| Upper-case severity | 202 | processed / NEW_ALERT | anna / sms |
| Epoch seconds | 202 | processed / NEW_ALERT | anna / sms |
| Extra nesting | 202 | needs_review / NEEDS_REVIEW | None observed |
| CSV with dotted headers | 202 | needs_review / NEEDS_REVIEW | None observed |
| Nested XML | 202 | processed / NO_MATCH | None observed |
| Timestamp without timezone | 202 | processed / NEW_ALERT | anna / sms |
| Typo in a rule field | 202 | Draft check HTTP 200, publish HTTP 200; cloned rule decision before/after the typo NEW_ALERT / NO_MATCH. The existing Anna rule remains active. | anna / sms |

[Observed requests and decisions](loadtest/results/w0-round3/final-1b82645640-release-final/m16.json) · [Final acceptance](docs/GATES.md)
<!-- w0-input-variations-end -->

<!-- w0-release-measurements-start -->
## Measured release results

The README sample produced its alert in **25.079 s** from a fresh clone (one timed run with warm Docker layers). The final runner passed **32/32 golden cases** and **13/13 console browser checks**. [Startup evidence](loadtest/results/w0-round3/final-1b82645640-release-final/first-alert.json) · [Golden runner](loadtest/results/w0-round3/final-1b82645640-release-final/golden.log) · [Browser receipts](loadtest/results/w0-round3/final-1b82645640-release-final/browser/result.json) · [Scorecard](docs/SCORECARD.md)
<!-- w0-release-measurements-end -->

---

## How it works

```
 HTTP · RabbitMQ · log files
            │
            ▼
   ingest ── durable inbox (PostgreSQL) ── unknown format → needs review
            │
            ▼
   rule workers (critical lane | normal lane)
            │  incident + notification written in one transaction
            ▼
   delivery worker ── email · SMS · phone (simulated) · live console (SSE)
            │
            ▼
   runbooks · approvals · audit
```

The rules we did not break:
- **Durable before acknowledge.** An event is committed before HTTP returns 202 or RabbitMQ gets its ACK.
- **Three identities, never mixed.** A retransmission is not a new event; 500 real failures are one incident counted 500 times; three send attempts are one notification.
- **Deterministic core.** Matching, routing and delivery never wait for an LLM.
- **Nothing silently dropped.** Unknown formats wait for review, a missing template value shows as `‹missing: path›`, and a critical alert always reaches at least one channel.

---

## What is simulated, and what is missing

- **Simulated:** the phone and SMS gateway (`mocks/fake_telecom`), email (Mailpit), operational actions (`mocks/mock_ops`) and sign-in. **Viewing as** is a demo switch, not authentication.
- **Not supported yet:** Excel files, CSV without a header row, non-JSON messages on RabbitMQ, events in Markdown.
- **Open targets:** normal p99 under 5 s (FAIL above), and the paired assist-on/off latency test (NOT_RUN).
- **For production:** real sign-in and permissions, an "assigned to me" view, and profiling of the per-message delivery cost.

---

## For developers

```sh
cp .env.example .env     # optional: AI assist settings
make infra               # PostgreSQL, RabbitMQ, Mailpit
cd backend && uv sync && cd ..
make test                # backend tests
make api                 # http://localhost:8000/health
cd frontend && npm ci && npm run dev
```

```
config/          sources, schemas, rules, teams, preferences, runbooks (hot-reloaded)
backend/alerting ingest, schemas, rules, routing, delivery, runbooks, assist, api
frontend/        React console
mocks/           fake telecom and mock ops services
loadtest/        golden runner, benchmarks and their raw results
fixtures/        sample events and golden cases from the task text
docs/            brief, API contract, gates, scorecard, scaling notes
```

[Brief](docs/BRIEF.md) · [API contract](docs/CONTRACT.md) · [Acceptance gates](docs/GATES.md) · [Scorecard](docs/SCORECARD.md) · [Scaling](docs/SCALING.md) · [Agent rules](AGENTS.md)

## Team

Built by Yi Yang ([@yy31104](https://github.com/yy31104), tech lead and backend), Zhongtian Dai ([@daizhongtian](https://github.com/daizhongtian), data, platform and console), zhaoqinghe ([@Petitaire](https://github.com/Petitaire), frontend) and Marlon Mejia ([@MarlonMejia15](https://github.com/MarlonMejia15), review and presentation), with AI coding agents. Every claim was checked against tests or measurements before it went in.
