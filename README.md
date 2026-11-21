# Alert Funnel — Flexible Alerting & Notification Service

Team workspace for the 2026 Goldman Sachs Warsaw Hackathon (Software Engineering track).

This local integration runs the React frontend, ingestion, rule workers, delivery services and simulators together. Start it from the repository root with Docker Desktop or Docker Compose.

```sh
docker compose up -d --build --wait
```

If you already have a database volume from `main`, use the same command. The
`db-upgrade` service applies the config registry and log checkpoint migrations
in one transaction before the API and workers start. Repeated runs preserve
existing events, rules and audit history. To check the upgrade, run
`docker compose logs db-upgrade`.

Open the frontend on port 5173 and the simulated phone on port 8100. The frontend uses the real API. The local acceptance report includes the browser demonstration, current test results, measured throughput and outage recovery. [Integration report](loadtest/results/dev2-integration/README.md)

### Send a first alert

From the repository root, send the supplied payments event. The demo clock puts Anna inside her daytime SMS preference in Warsaw.

```sh
curl --fail-with-body -sS http://127.0.0.1:8000/v1/events/generic-webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: demo-generic-key' \
  -H 'X-Demo-Clock: 2026-10-23T11:15:00+02:00' \
  --data-binary @fixtures/events/alertmanager_high_error_rate.json
```

Copy the first ID from `event_ids` into `GET /v1/events/<id>/explain`. Once processing finishes, look for the `anna-payments-critical` decision and the notification for `anna` on `sms`. The simulated phone records the delivery. [Example event](fixtures/events/alertmanager_high_error_rate.json)

## Performance and scalability

The **[benchmark report](loadtest/results/saturation-ae69a28/REPORT.md)** contains the results from 24 saturation runs and six controlled repeats, with charts, test commands, machine specs, and matched Docker Compose and benchmark terminal captures.

### Throughput and latency

We ran the saturation matrix on source commit `ae69a28` with 24 Docker vCPUs and 31.06 GiB of Docker memory. Each run scheduled 700, 1,400, 2,800 or 5,600 synthetic normal RabbitMQ events/s for 60 seconds, plus two critical events/s. We tested one hot alert key and 1,000 distinct keys with one, two and four normal rule workers. End-to-end latency covers the time from the actual send, through broker wait and alert processing, to receipt by the simulated SMS or phone provider. [Machine and images](loadtest/results/saturation-ae69a28/machine.json)

| Normal rule workers | First growing backlog with one hot key | First growing backlog with 1,000 keys |
|---:|---:|---:|
| 1 | 5,600/s | 700/s |
| 2 | 5,600/s | 1,400/s |
| 4 | 5,600/s | 2,800/s |

The single-key workload kept up at the tested rate of **2,800 normal events/s**. With 1,000 keys and four rule workers, the highest tested rate with a steady inbox was **1,400 normal events/s**. Peak sustained processing in the matrix reached 3,629 events/s with one hot key while backlog was growing. Of 3,782,880 scheduled events, RabbitMQ confirmed 3,775,341. The generator dropped 7,539 at the highest rate. Ingestion rejected zero. [Per-run results](loadtest/results/saturation-ae69a28/runs.csv)

![Measured throughput by normal rule worker count and key shape](loadtest/results/saturation-ae69a28/throughput.png)

At **1,400 normal events/s across 1,000 alert keys**, changing only the normal rule worker count gave the results below. Ingested events/s uses the full 60-second offer window. Sustained processing counts completions in the final 50 seconds, including work queued during warm-up. Both rates include critical events. Pending inbox figures are early and late medians in the steady window, and N counts verified receiver receipts. [Run metrics](loadtest/results/saturation-ae69a28/runs.csv)

| Normal rule workers | Ingested/s | Sustained processed/s | Pending inbox, early → late | Normal p50 / p99 | Critical p50 / p99 |
|---:|---:|---:|---:|---:|---:|
| 1 | 1,402.0 | 626.0 | 15,790 → 41,995 | 22,565 / 38,499 ms (N=2,000) | 201 / 345 ms (N=240) |
| 2 | 1,402.0 | 1,170.0 | 6,130 → 14,196 | 25,034 / 42,449 ms (N=2,000) | 207 / 371 ms (N=240) |
| 4 | 1,402.0 | 1,405.9 | 410 → 384 | 26,689 / 45,547 ms (N=2,000) | 203 / 359 ms (N=240) |

RabbitMQ kept up in all three runs. No generator drops or ingestion rejects occurred, and all 2,240 expected simulated deliveries arrived in each run. Four normal workers kept the rule inbox steady. The burst of first notifications still queued at the shared delivery worker, which is why the normal receiver p99 stayed high. [Stage measurements](loadtest/results/saturation-ae69a28/REPORT.md#bottleneck-evidence)

The four-worker run wrote 4,000 rule decisions and made 26.6 SMS or phone delivery attempts per 1,000 processed events. [Run summary](loadtest/results/saturation-ae69a28/m11-11210120-1d623-r1400-k1000-w4/summary.json)

At a target of 5,600 normal events/s with one hot key, adding a second RabbitMQ intake consumer raised measured ingress from 3,571.0 to 5,546.8 events/s in the repeat. The late-window RabbitMQ queue median fell from 99,609 to 158 while the rule inbox began to grow. The result tells us which stage to scale first for that workload. [Controlled comparison](loadtest/results/saturation-ae69a28/bottleneck-analysis.md)

Six controlled runs repeated the bottleneck checks. In the delivery comparison at 700 normal events/s across 1,000 keys, scaling delivery from one to four workers reduced normal receiver p99 from 39.9 to 33.6 seconds (N=2,000 per run). This measures the notification burst separately from rule throughput. [All controlled runs](loadtest/results/saturation-ae69a28/REPORT.md#controlled-diagnostic-repeats)

Run the same matrix from the repository root with Python 3.12 and Docker Compose.

```sh
docker compose pull postgres rabbitmq mailpit
python loadtest/run_saturation.py --output loadtest/results/saturation-REPEAT
```

The report includes the p50, p95 and p99 with receipt counts, backlog trends, PostgreSQL CPU and lock samples, and the two terminal captures from the same run. [Load-test script](loadtest/run_saturation.py) · [Recorded commands](loadtest/results/saturation-ae69a28/reproduce.txt) · [Evidence checks](loadtest/results/saturation-ae69a28/verification.json)

### How the system can grow

The tests reproduced independent scaling of RabbitMQ consumers, normal rule workers, and delivery workers on one Docker host. Normal workers claim separate inbox rows in PostgreSQL. Critical events use a separate rule worker and share the intake and delivery paths. [Controlled results](loadtest/results/saturation-ae69a28/REPORT.md#controlled-diagnostic-repeats)

- **Rules.** PostgreSQL stores active versions of rules and other configuration for workers to load. [Rule configuration loading](backend/alerting/rules/storage.py)
- **Deduplication and cooldown.** Inbox uniqueness uses the source and source event ID. Alert counts and notification cooldowns are stored per rule and alert key. A transaction lock serializes updates to the same group across workers. [Inbox deduplication](backend/alerting/ingest/store.py) · [Rule worker claims and locks](backend/alerting/rules/worker.py)
- **Delivery.** PostgreSQL stores pending notifications, claim expiry, retry times, and attempt history. Another sender can claim work after an expired lease. [Delivery worker](backend/alerting/delivery/worker.py)

The RabbitMQ broker and PostgreSQL instance are each a single point of failure in the tested setup. A broker outage interrupts AMQP intake. Loss of PostgreSQL pauses durable intake, rule processing, and delivery updates.

The roadmap uses 1,400 normal events/s across 1,000 keys as its reference workload. The multiples below are future test targets. [Reference run](loadtest/results/saturation-ae69a28/m11-11210120-1d623-r1400-k1000-w4/summary.json)

1. **10× traffic.** Scale the worker stage with growing backlog across hosts, give critical traffic its own intake queue, and test batched database writes and index changes. Compare queue age, throughput, p99, PostgreSQL CPU, and lock waits.
2. **100× traffic.** Partition queues and durable state by tenant or source. Route updates for the same rule and alert key to one owner. Check deduplication across retries and replays, and test fairness and per-channel delivery quotas.
3. **1,000× traffic.** Distribute tenants across isolated deployments with replicated brokers and PostgreSQL failover. Test recovery of inbox, alert, and outbox state while traffic continues.

A future validation scenario offers one million events over 60 seconds with 10,000 active subscriptions. Measure drain time and receiver p99 by priority, and reconcile accepted event IDs and expected notification receipts after failover.

## Team: start here
1. Read [docs/BRIEF.md](docs/BRIEF.md) (what, scope, gates, ownership), then [docs/CONTRACT.md](docs/CONTRACT.md).
2. Read [AGENTS.md](AGENTS.md), then your handoff:
   - Yi — backend core + tech lead: [DEV1-backend-Yi.md](docs/handoff/DEV1-backend-Yi.md) · branch `feat/backend-yi`
   - daizhongtian — data & platform: [DEV2-data-daizhongtian.md](docs/handoff/DEV2-data-daizhongtian.md) · branch `feat/data-daizhongtian`
   - zhaoqinghe — frontend: [DEV3-frontend-zhaoqinghe.md](docs/handoff/DEV3-frontend-zhaoqinghe.md) · branch `feat/frontend-zhaoqinghe`
   - Marlon — review & presentation (from G3): [DEV4-review-Marlon.md](docs/handoff/DEV4-review-Marlon.md) · branch `feat/review-marlon`
3. `git switch <your branch>`. Never push to `main`; open a PR.
4. Run the base:
   ```bash
   cp .env.example .env          # add ANTHROPIC_API_KEY locally if you work on an assist feature
   make infra                    # postgres + rabbitmq + mailpit (docker)
   cd backend && uv sync && cd ..
   make test                     # scaffold tests must stay green
   make api                      # http://localhost:8000/health
   ```
5. Record gate results in [docs/GATES.md](docs/GATES.md) (PASS / FAIL / NOT_RUN).

## Layout
```
config/            sources, schemas, rules, teams/preferences, runbooks (hot-reloaded)
fixtures/events/   Appendix A sample events (+ a pricing log file)
fixtures/golden/   black-box acceptance cases derived from the task text
fixtures/api/      mock API responses for UI work
db/migrations/     001 shared; 1xx Yi, 2xx daizhongtian
backend/alerting/  contracts.py, llm.py, templates.py, clock.py, db.py (shared) + ingest/ schemas/ config/ rules/ routing/ delivery/ runbooks/ assist/ api/
mocks/             fake_telecom, mock_ops (daizhongtian)
loadtest/          golden runner, load/chaos reports (daizhongtian)
frontend/          React app (zhaoqinghe)
```

## Local URLs
API `:8000` · UI `:5173` · Mailpit `:8025` · RabbitMQ `:15672` · fake-telecom `:8100` · mock-ops `:8200`
