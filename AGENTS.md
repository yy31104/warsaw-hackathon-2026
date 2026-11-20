# AGENTS.md — rules for every coding agent (Codex, Claude Code, Copilot) in this repo

1. **Read first:** `docs/BRIEF.md`, `docs/CONTRACT.md`, and your person's handoff in `docs/handoff/`: `DEV1-backend-Yi.md`, `DEV2-data-daizhongtian.md`, `DEV3-frontend-zhaoqinghe.md` or `DEV4-review-Marlon.md`.
2. **Stay in your lane.** Edit only the directories listed under "You own" in your handoff. Do **not** edit shared files: `docs/CONTRACT.md`, `AGENTS.md`, `backend/alerting/contracts.py`, `llm.py`, `templates.py`, `clock.py`, `db.py`, `api/app.py`, `db/migrations/001_init.sql`, `.github/`. If one of them needs a change, stop and tell your person; Yi (tech lead) makes it.
3. **Migrations:** only add new files, and only in your range: `1xx` Yi, `2xx` daizhongtian. Never edit an existing migration.
4. **Pure first.** Write the pure function and its tests (using `fixtures/`) before wiring up the DB, queues or HTTP. Never call an LLM or make an HTTP call inside a DB transaction. Never use `datetime.now()` in logic; use `alerting.clock`.
5. **Three identities** (CONTRACT §2) are never mixed: event identity, alert key, notification id.
6. **Tests:** `make test` must pass before every commit. Put your tests in `backend/tests/<your area>/` (backend) or `frontend/` (UI).
7. **Git:** work only on your person's branch (`feat/backend-yi`, `feat/data-daizhongtian`, `feat/frontend-zhaoqinghe`, `feat/review-marlon`). Make small commits with conventional messages (`feat(data): …`). Never push to `main`; open a PR. Never commit `.env` or keys.
8. **Honesty:** report exactly what you ran and what you did not run. Never present an estimate as a measurement. Mark results PASS / FAIL / NOT_RUN.
9. **Data:** synthetic only. Nothing goes to an LLM except through `alerting/llm.py`, which redacts and times out.
