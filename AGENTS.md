# AGENTS.md — rules for every coding agent (Codex, Claude Code, Copilot) in this repo

1. **Read first:** `docs/BRIEF.md`, `docs/CONTRACT.md`, and the handoff for your developer in `docs/handoff/` (DEV1-platform, DEV2-ingestion, DEV3-incidents, DEV4-notifications, UI, REVIEW).
2. **Stay in your lane.** Edit only the directories listed under "You own" in your handoff. Do **not** edit shared files: `docs/CONTRACT.md`, `backend/alerting/contracts.py`, `llm.py`, `templates.py`, `clock.py`, `db.py`, `api/app.py`, `db/migrations/001_init.sql`, `docker-compose.yml`, `.github/`. If one needs a change, stop and report it to your developer.
3. **Migrations:** add new files only, in your range (`0xx` DEV1, `1xx` DEV2, `2xx` DEV3, `3xx` DEV4). Never edit an existing migration.
4. **Pure first.** Write the pure function and its tests (using `fixtures/`) before wiring DB, queues or HTTP. Never call an LLM or make HTTP calls inside a DB transaction. Never use `datetime.now()` in logic; use `alerting.clock`.
5. **Three identities** (CONTRACT §2) are never mixed: event identity, alert key, notification id.
6. **Tests:** `make test` must pass before every commit. Add tests in `backend/tests/<your area>/`.
7. **Git:** work on your branch only (`feat/platform`, `feat/ingestion`, `feat/processing`, `feat/delivery`, `feat/frontend`). Small commits with conventional messages (`feat(ingestion): …`). Never push to `main`. Never commit `.env` or keys.
8. **Honesty:** report exactly what you ran and what you did not run. Never present an estimate as a measurement. Use PASS / FAIL / NOT_RUN.
9. **Data:** synthetic only. Send nothing to an LLM except through `alerting/llm.py` (it redacts and times out).
