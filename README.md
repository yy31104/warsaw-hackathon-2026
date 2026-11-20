# Alert Funnel — Flexible Alerting & Notification Service

Team workspace for the 2026 Goldman Sachs Warsaw Hackathon (Software Engineering track).

> **Status: G0 scaffold.** Contracts, configs, fixtures and the backend skeleton are in place; features are being built on the four role branches. This README becomes the judges' README at G4.

## Team: start here
1. Read [docs/BRIEF.md](docs/BRIEF.md) (what, scope, gates, ownership), then [docs/CONTRACT.md](docs/CONTRACT.md).
2. Open your handoff: [DATA](docs/handoff/DATA.md) · [RULES](docs/handoff/RULES.md) · [DELIVERY](docs/handoff/DELIVERY.md) · [UI](docs/handoff/UI.md).
3. Check out your branch: `data/platform`, `rules/engine`, `delivery/runbooks`, `ui/frontend`.
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
db/migrations/     001 shared; 0xx DATA, 1xx RULES, 2xx DELIVERY
backend/alerting/  contracts.py, llm.py, templates.py, clock.py, db.py (shared) + ingest/ schemas/ config/ rules/ routing/ delivery/ runbooks/ assist/ api/
mocks/             fake_telecom (DATA), mock_ops (DELIVERY)
loadtest/          load generator, collector, reports (DATA)
frontend/          React app (UI)
```

## Local URLs
API `:8000` · UI `:5173` · Mailpit `:8025` · RabbitMQ `:15672` · fake-telecom `:8100` · mock-ops `:8200`
