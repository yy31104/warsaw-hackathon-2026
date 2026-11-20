# Playbook — how to build this well with AI agents

Practical habits from our earlier projects and competitions, with what each one achieved.
Read §1 once, then the section for your role.

## 1. Working with coding agents (everyone)

**One task per prompt, plan first, tests first.** Give the agent one numbered task from your handoff. Ask for a short plan, then the tests (built from `fixtures/`), then the code. Commit after each task. Big multi-task prompts produce a lot of code that has never been run.

**Pick the model by the kind of work:**

| Work | Use | Why |
|---|---|---|
| Core logic with concurrency or money-like correctness: engine, worker transaction, delivery leases, dedup, approvals | The strongest model at high reasoning (Claude Code on Claude Opus 5.5, or Codex's top model at high effort) | Mistakes here are silent and expensive |
| Boilerplate: CRUD routes, forms, table components, test data, Dockerfiles | A faster model (Claude Sonnet 5.5 / Haiku 5.5, or Codex default) | Same quality, much faster |
| Review of a finished diff | **A different model from the one that wrote it** | It doesn't share the writer's blind spots |

**Two-model loop (our usual workflow: one model researches, another reviews, a third implements):** let Codex implement a task, then have Claude Code review the diff before you open the PR (or the other way round). A generator checking its own work repeats its own wrong assumptions. That is exactly how the HackerRank run went from dev 17/17 to holdout 3/8.

**Claude Code skills worth using here:**
- `/code-review`: correctness review of your branch diff before every PR.
- `/security-review`: the ingest endpoints, API keys, approval codes, action allow-lists (SSRF), anything that executes runbook steps.
- `/simplify`: after a feature works, a cleanup pass (reuse, dead code). Never before it works.
- `run`: actually start the app and check the change in the running system, not only in unit tests.
- `claude-api`: before writing any LLM call (correct model IDs, structured outputs, error handling).
- `dataviz`: before building the dashboard charts.
- `vibe-debrief`: at the end of a session, explains what the agent built and why. Use it so **you** can explain your own part to the judges in Q&A.

**Never accept "done" from an agent.** Done means: it runs from the integrated `main`, and you saw it work, or a test proves it. Ask the agent for a PASS / FAIL / NOT_RUN table with the command that proves each line.

**Give the failing input, not a description.** When something breaks, paste the exact event, request, log line or stack trace. Agents fix concrete failures well and guess badly from summaries.

## 2. Techniques that worked before

| Technique | Where it worked | Effect | Apply here |
|---|---|---|---|
| Idempotency key + conflict detection (same key with a different body = conflict, recorded) | LedgerOps payments | Retries never doubled an effect | Event identity, notification `dedup_key` (Yi, daizhongtian) |
| Outbox + separate attempts table; external call made **outside** the transaction | LedgerOps payouts | Crash-safe sends with a full audit trail | `notifications` + `delivery_attempts` (Yi) |
| An independent simulator process with its own records | LedgerOps `provider_sim` | Delivery proven by the receiver's receipts, not by our own DB | fake-telecom, Mailpit (daizhongtian) |
| Fault flags at exact crash points + `kill -9` | LedgerOps worker recovery | "No loss" was demonstrated, not claimed | `FAULT_AFTER_COMMIT_BEFORE_ACK`, delivery worker killed mid-send (daizhongtian, Yi) |
| Independent reconciliation over a second path | LedgerOps export/verify | Totals checked by something other than the code under test | Load test: sent = delivered + failed + pending, lost = 0 (daizhongtian) |
| Mutation check: break one line on purpose and confirm a test fails | LedgerOps selected mutations | Proved the tests actually bite | Dedup conflict, cooldown, `[08:00,20:00)` boundary, `> 10,000,000` (Yi) |
| Pure tests separate from DB tests | Chronicle (pure suite + Postgres suite) | Fast feedback loop plus real-DB confidence | Pure engine/plan/normalise tests; DB tests skip when Postgres is down (all backend) |
| Offline eval set with deterministic checks for an LLM feature | AI PR Review (offline cases + checks) | LLM quality reported as n/N, not as vibes | 8–10 schema samples with expected aliases; runbook texts with expected actions (daizhongtian) |
| Holdout kept away from development; no special-casing of samples | HackerRank Orchestrate | Catches assumptions shared by builder and checker | Never hardcode sample formats; Marlon's blind test (everyone) |
| Fixtures as the API contract + screenshots at real resolution | CASCADE X frontend | No late mismatch between UI and backend; layout checked in the real app | `fixtures/api` updated in the same commit; 1440×900 screenshots (zhaoqinghe) |
| Show where data is missing before showing model output | Striga FixWatch evaluation | Users see what the model never got to see | Onboarding page shows missing fields and unrouted counts first (daizhongtian, zhaoqinghe) |

## 3. Role tips

**Yi — backend core**
- Keep `dsl.compile`, `engine.evaluate`, `routing.plan` and `runbooks.render` pure and stable. Other people call them.
- Concurrency test: 2 workers, 500 events with one alert key → exactly 1 alert, count 500, 1 page. This is the strongest "scalability" evidence we have.
- Put every external send behind claim → commit → send → record. If a reviewer finds an HTTP call inside a transaction, it's a bug.

**daizhongtian — data & platform**
- Build the normaliser **table-driven**: one row per sample file, with the expected schema, severity, aliases and identity. Each new format becomes a new row, not new code.
- For the LLM: send `llm.describe_sample(fields)` (paths, types, redacted examples), never the raw JSON string. Redaction on raw text breaks JSON (e.g. a millisecond timestamp gets masked). Cache per fingerprint. Report suggestion accuracy n/N on your eval set.
- Measurement: k6 `constant-arrival-rate` (report `dropped_iterations`). Always publish N, the machine spec and the raw JSON next to any p50/p99.

**zhaoqinghe — frontend**
- Mantine + TanStack Query. Build every screen with its **loading, empty and error** states. On video, a blank page looks broken.
- Write copy for non-engineers: one plain sentence per rule ("When a prod settlement fails, call the on-call and text them"), short labels, no jargon in buttons.
- `EventSource` reconnects by itself; show the "Live / reconnecting" state in the header.
- After each screen, screenshot it at 1440×900 in the real browser (the `run` skill, or open it yourself) and post it to the team.

**Marlon — review**
- Test like a judge: fresh clone, README only, no help, with a stopwatch. Every hesitation is a finding.
- Feed unseen formats through the UI and record LLM-suggested vs correct fields (n/N) and minutes to the first correct alert.

## 4. LLM in the product (the rules)
- Off the alert path. It only produces **drafts** (schema mapping, runbook steps, rule drafts, fix proposals). A human confirms in the UI. Audit records `source: llm|heuristic`.
- Always use `alerting/llm.py`: structured output (Pydantic), 20 s timeout, `None` on any failure → deterministic fallback with the same UI.
- Model: `ASSIST_MODEL` (default `claude-opus-5-5`). The key lives only in your local `.env`.
