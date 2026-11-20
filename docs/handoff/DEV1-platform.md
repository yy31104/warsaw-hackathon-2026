# DEV1 — Tech lead: platform, database, integration, evidence · Yi

**Branch:** `feat/platform` (shared-file changes go to `main` through small PRs that you announce)
**You own:** shared files (`docs/CONTRACT.md`, `backend/alerting/contracts.py`, `llm.py`, `templates.py`, `clock.py`, `db.py`, `api/app.py`, `api/routes_ops.py`), `db/migrations/001_init.sql` + `0xx`, `docker-compose.yml`, `backend/Dockerfile`, `.github/workflows/`, `AGENTS.md`, `mocks/mock_ops/`, `loadtest/`, `docs/GATES.md`; second track `frontend/` (see BRIEF §5)

## The base is laid (commits on `main`)
Contract, shared types, DB schema (inbox, incidents, outbox, attempts, actions, audit, proposals), config for the four example rules and three runbooks, golden cases, mock API fixtures, FastAPI app with every router wired, compose (postgres, rabbitmq, mailpit), CI, AGENTS.md, branches. Scaffold tests pass.

## Tasks

### Now → G0 (21:15)
1. Add every teammate as a collaborator on GitHub (Settings → Collaborators; you need their usernames). Send each person their handoff link.
2. Walk the team through BRIEF §3 (roles + seams) and CONTRACT §2 (three identities) in 10 minutes. Settle the NOTIFY payload shapes between DEV3 and DEV4.
3. Ask a mentor: deadline, Lockbox path, meaning of 2.4 "Propose fixes".
4. With DEV2, check `fixtures/golden/expectations.yaml` line by line against the printed task.

### G0 → G1 (00:00)
5. **Frontend kickoff** (BRIEF §5): start an agent on `docs/handoff/UI.md` screens 1–2 against `fixtures/api/`, on `feat/frontend`.
6. **Full compose:** `backend/Dockerfile` (uv, python 3.12). Add services api, ingest-amqp, ingest-logtail (DEV2), rule-worker ×2 + scheduler (DEV3), delivery-worker (DEV4), fake-telecom (DEV4), mock-ops (yours), and frontend. Healthchecks and `depends_on` included.
7. **mock-ops** `mocks/mock_ops/` (:8200, network alias `counterparty-service`): `GET /health` (toggle), `POST /workflows/{id}/retry` (fail N times, then succeed), `POST /services/{name}/restart`, `POST /tickets`.
8. **Integration:** merge PRs as they come (CI green, quick read). At ~23:30 run the G1 slice yourself from `main`. Record it in GATES.md.

### G1 → G3
9. **Golden runner** `loadtest/golden_runner.py`: a black box over the public API, fake-telecom `/messages` and the Mailpit API, following the header of `expectations.yaml`. Reset between cases (`/v1/debug/reset`). Writes a PASS/FAIL table into GATES.md.
10. `routes_ops`: `/metrics`, `GET /v1/stats/funnel`, `POST /v1/debug/reset` (DEMO_MODE).
11. **Load + chaos report** `loadtest/`:
    - k6 `constant-arrival-rate` against HTTP, plus DEV2's `tools/publish.py --rate` for AMQP.
    - Collector: ingest/s, processed/s, delivered/s, e2e p50/p95/p99 with N, backlog over time, failures.
    - Compare 1 vs 2 rule-workers.
    - Chaos run: fake-telecom `down` for 30 s → reconciliation shows 0 lost.
    - Raw JSON and the machine spec go in `loadtest/results/`.
12. **Holdout at G3:** `../private-holdout/` (outside the repo). You don't write the engine, so you may run it. **Don't share it with DEV2–4 or their agents.**
13. Hand the late-joining teammate `docs/handoff/REVIEW.md` at G3.

## Merge rules you enforce
- CI must be green.
- PRs stay inside the owner's directories; shared files change only through your PRs.
- Migration ranges: 0xx you, 1xx DEV2, 2xx DEV3, 3xx DEV4.
- A feature counts as done only when it runs from `main`.
