# Handoff — UI (Frontend)

**Branch:** `ui/frontend` · **Layers:** L6 user experience, funnel dashboard
**You own:** `frontend/` (everything)
**Read first:** BRIEF §1, §9 (storyboard: build the screens in this order), CONTRACT §9 (API), §10 (SSE), `fixtures/api/README.md`

## Mission
Someone who has never seen the product must be able to (a) watch alerts arrive live, (b) understand why they got one and what to do, (c) change a rule or a preference and see its impact *before* saving, and (d) onboard a new event format without code. The UX is judged directly ("usable by someone who has never seen it", "is the rule editor understandable"), and the video is made of your screens.

## Stack
Vite + React + TypeScript + **Mantine** (fast, good defaults, dark mode) + TanStack Query + `EventSource` for SSE. API base `http://localhost:8000`. A user switcher in the header sets `X-User` on mutating calls (simulated auth: anna, sam, lee, rita, pat, cara, paul, ops).

**Until G1, build against `fixtures/api/*.json`** (a mock-mode flag in `src/api.ts`, plus a tiny fake SSE that replays `fixtures/api/stream.ndjson`). Switch to the real API per screen as the backend lands.

## Screens (in order)

### By G1 (Sat 00:00), on mocks
1. **Live feed** (`/`, team selector): alert cards in the rule's `color`, with title, severity badge, **live occurrence counter**, status, age, team, score. New cards slide in through SSE (`alert.created` / `alert.updated`). Filters: status, severity, team, text. Show a "Live" indicator with the last-event time.
2. **Alert detail** (`/a/:id`, which is also the SMS short link):
   - header with Ack / Resolve, and the fields chosen by `present.fields`;
   - occurrences sample (table);
   - **timeline**: decisions, notifications (state + attempts, e.g. "SMS to sam: retrying (attempt 3, gateway down) → delivered"), actions, audit;
   - "Why did I get this?" → explain view (rule trace with pass/fail per condition).

### By G2 (Sat 04:00), wired to the real API
3. **Runbook panel** on alert detail: rendered steps with filled params.
   - Action steps get a **Run** button (`approval: none`) or **Request approval** (`required`) → shows "Waiting for lee's approval (code 4821)", then the approver sees **Approve / Reject**.
   - Markdown is rendered; checklists are tickable (recorded in audit).
   - Free-text runbooks show the text plus a **"Structure with AI"** button → editable steps → save.
4. **Rules** (`/rules`): list with owner, origin badge (`file` = read-only), version, matches in the last hour.
   - **Editor:** a form-based condition builder (field autocomplete from recent events' paths and aliases, op dropdown, nested AND/OR groups), a route editor (to + channels or "preferences"), and presentation (title/body templates with a **live preview rendered on a sample event**, fields, colour). The YAML tab stays in sync.
   - A plain-English sentence at the top: "When … then notify … via …".
   - **Test** against a pasted or selected event → per-condition trace.
5. **Preferences** (`/people`): per user, ordered rows of time window + min severity + channels (Anna is the demo). Team defaults. A small "who gets what" matrix by severity.

### By G3 (Sat 07:30), P0.5/P1
6. **Onboarding / New formats** (`/onboarding`): list of `schema-issues` (fingerprint, count, last seen, sample).
   - Click one → flattened field table (path, type, sample value) + an alias mapping form **prefilled from `POST /v1/schemas/suggest`** (badge "AI suggestion" or "heuristic", per-field rationale on hover) → **Confirm** → `POST /v1/schemas`.
   - Then "Create rule from this sample" → the rules editor prefilled.
   - Show a stopwatch from first seen to the first routed alert; we film it.
7. **Impact preview / Propose fix:**
   - In the rule editor, **Preview impact** → `POST /v1/rules/{id}/preview` → diff cards: alerts and notifications before → after (by recipient/channel), and a red list "events that would no longer notify anyone" with severity.
   - On an alert card, **Propose fix** → free text or "Suggest with AI" → proposal with preview → the owner approves in `/proposals`.
8. **Funnel dashboard** (`/dashboard`): raw → accepted → grouped into alerts → notified → critical (from `/v1/stats/funnel` + `stats.tick`), plus ingest/s, processed/s, backlog, oldest pending, notifications by state, retries.
9. **NL rule box** on `/rules`: "Describe the alert you want" → `POST /v1/rules/draft` → opens the editor with the draft and the plain-English summary.

## Acceptance (Yi checks)
- A teammate who has not seen the UI completes the "change Anna's SMS window to 09:00–18:00" and "create a rule for trade.rejected on desk fx" tasks unaided (time it).
- The counter increments without a refresh.
- Every Appendix B runbook shows usable steps.
- File-origin rules can't be edited (clear message).

## Pitfalls
- Don't poll for the feed. Use SSE (task requirement: push, not page refresh). Re-fetch details on events.
- Don't hide `‹missing: …›` in titles; show it. It's a feature (visible data gaps).
- Keep the demo laptop at 1440×900. Large fonts on cards; the judges watch a video.

## Starter prompt for your coding agent
> You are implementing the UI role of the Alert Funnel hackathon project. Read `docs/BRIEF.md`, `docs/CONTRACT.md` (§9, §10), `docs/handoff/UI.md` and `fixtures/api/README.md` fully first. Work only inside `frontend/`. Scaffold Vite + React + TypeScript + Mantine + TanStack Query. Build screens 1 and 2 against the mock fixtures in `fixtures/api/` with a mock-mode switch in `src/api.ts` and a fake SSE replay of `fixtures/api/stream.ndjson`. Keep API calls typed from the shapes in the fixtures. Commit after each screen. Report what you ran and what you did not run.
