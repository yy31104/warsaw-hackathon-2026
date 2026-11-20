# Alert Funnel — Flexible Alerting & Notification Service

Team workspace for the 2026 Goldman Sachs Warsaw Hackathon (Software Engineering track).

This local integration runs the React frontend, ingestion, rule workers, delivery services and simulators together. Start it from the repository root with Docker Desktop or Docker Compose.

```sh
docker compose up -d --build --wait
```

Open the frontend on port 5173 and the simulated phone on port 8100. The frontend uses the real API. The local acceptance report includes the browser demonstration, current test results, measured throughput and outage recovery. [Integration report](loadtest/results/dev2-integration/README.md)

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
