# Alert Funnel — Flexible Alerting & Notification Service

Team workspace for the 2026 Goldman Sachs Warsaw Hackathon (Software Engineering track).

> **Status: G0 scaffold.** Contracts, configs, fixtures and the backend skeleton are in place; features are being built on the four role branches. This README becomes the judges' README at G4.

## Team: start here
1. Read [docs/BRIEF.md](docs/BRIEF.md) (what, scope, gates, ownership), then [docs/CONTRACT.md](docs/CONTRACT.md).
2. Read [AGENTS.md](AGENTS.md), then your handoff: [DEV1 platform](docs/handoff/DEV1-platform.md) · [DEV2 ingestion](docs/handoff/DEV2-ingestion.md) · [DEV3 incidents](docs/handoff/DEV3-incidents.md) · [DEV4 notifications](docs/handoff/DEV4-notifications.md) · [UI](docs/handoff/UI.md) · [Review & present](docs/handoff/REVIEW.md).
3. Switch to your branch: `feat/platform`, `feat/ingestion`, `feat/processing`, `feat/delivery`, `feat/frontend`. Never push to `main`; open a PR.
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
db/migrations/     001 shared; 0xx DEV1, 1xx DEV2, 2xx DEV3, 3xx DEV4
backend/alerting/  contracts.py, llm.py, templates.py, clock.py, db.py (shared) + ingest/ schemas/ config/ rules/ routing/ delivery/ runbooks/ assist/ api/
mocks/             fake_telecom (DEV4), mock_ops (DEV1)
loadtest/          golden runner, load/chaos reports (DEV1)
frontend/          React app (UI)
```

## Local URLs
API `:8000` · UI `:5173` · Mailpit `:8025` · RabbitMQ `:15672` · fake-telecom `:8100` · mock-ops `:8200`
