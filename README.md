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
   cp .env.example .env          # optional assist uses ASSIST_PROVIDER, ASSIST_MODEL and the selected provider's API key
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
