# DEV3 · zhaoqinghe — Frontend

**Branch:** `feat/frontend-zhaoqinghe` · **GitHub:** `Petitaire`
**You own:** `frontend/` (everything)
**Read first:** BRIEF §1, §7 (the storyboard — build the screens in this order), CONTRACT §9 (API + owners), §10 (SSE), `fixtures/api/README.md`

## Mission
Someone who has never seen the product must be able to:
- (a) watch alerts arrive live,
- (b) understand why they got one and what to do,
- (c) change a rule or a preference and see its impact *before* saving,
- (d) onboard a new event format without code.

The UX is judged directly ("usable by someone who has never seen it", "is the rule editor understandable"). The video is made of your screens.

## Stack
Vite + React + TypeScript + **Mantine** + TanStack Query + `EventSource` for SSE. API base `http://localhost:8000`. A user switcher in the header sets `X-User` on mutating calls (simulated auth: anna, sam, lee, rita, pat, cara, paul, ops).

**Start now on mocks.** Put a mock-mode flag in `src/api.ts` that serves `fixtures/api/*.json`, plus a fake SSE that replays `fixtures/api/stream.ndjson`. Switch to the real API screen by screen as the backend lands. Who serves what:
- **Yi:** incidents, notifications, runbook steps, actions, approvals.
- **daizhongtian:** rules CRUD and preview, schemas and onboarding, preferences, SSE, funnel.

## Screens (in order)

### By G1 (Sat 00:00), on mocks
1. **Live feed** (`/`, with a team selector):
   - Incident cards in the rule's `color`, showing title, priority/severity badge, **live occurrence counter**, status, age, team and score.
   - New cards slide in via SSE.
   - Filters: status, severity, team, text.
   - A "Live" indicator.
2. **Incident detail** (`/a/:id`, which is also the SMS short link):
   - Ack / Resolve buttons.
   - The fields listed in `present.fields`.
   - An occurrences table.
   - A **timeline**: decisions, notifications with state + attempts, actions, audit.
   - "Why did I get this?" → the explain view with a pass/fail trace per condition.

### By G2 (Sat 04:00), wired to the real API
3. **Runbook panel** inside incident detail:
   - Steps rendered with filled-in values.
   - **Run** button for `approval: none`. **Request approval** for `required`, which shows "Waiting for lee (code 4821)"; the approver sees Approve / Reject.
   - Markdown rendered; checklist ticks are written to the audit log.
   - Free-text runbooks show a **"Structure with AI"** button → editable steps → save.
3b. **Propose fix (task 2.4, P0):** a "Propose fix" button on every incident → text box → `POST /v1/proposals {alert_id, text}` (with `X-User`) → shows "Proposal sent to <rule owner>". A `/proposals` list for owners. Preview and AI suggestion come later (screen 7).
4. **Rules** (`/rules`):
   - List with owner, an origin badge (`file` = read-only), version, and matches in the last hour.
   - Editor:
     - condition builder with field autocomplete from recent events, op dropdown, nested AND/OR;
     - route editor;
     - presentation editor with a **live title preview on a sample event**;
     - a synced YAML tab.
   - A plain-English summary at the top: "When … notify … via …".
   - **Test against an event**: shows the trace.
5. **Preferences** (`/people`):
   - Per user: ordered rows of time window + min severity + channels (Anna is the demo).
   - Team defaults.
   - A "who gets what" matrix.

### By G3 (Sat 07:30)
6. **Onboarding** (`/onboarding`):
   - List of schema issues. Click one to see a flattened field table plus an alias form **prefilled from `POST /v1/schemas/suggest`** (badge AI/heuristic, rationale on hover).
   - **Confirm** → `POST /v1/schemas`, then "Create rule from this sample".
   - A stopwatch from first seen to the first routed alert, for the video.
7. **Impact preview / Propose fix:**
   - In the rule editor, **Preview impact** shows before → after cards: incidents and notifications per recipient/channel, plus a red list of "events that would no longer notify anyone".
   - On an incident, **Propose fix** opens a free-text box or "Suggest with AI", producing a proposal; the owner approves it.
8. **Funnel dashboard** (`/dashboard`):
   - Funnel: raw → accepted → grouped → notified → critical.
   - Also ingest/s, processed/s, backlog, oldest pending, notification states, retries.
9. **NL rule box**: "Describe the alert you want" → draft opens in the editor.

## Acceptance
- A teammate who has never seen the UI changes Anna's SMS window to 09:00–18:00 and creates a rule for `trade.rejected` on desk fx, without help (time it).
- The counter increments with no refresh.
- Every Appendix B runbook shows usable steps.
- File-origin rules can't be edited.

## Pitfalls
- No polling for the feed: use SSE. The task requires push, not refresh.
- Show `‹missing: …›` in titles; it's a feature.
- Demo at 1440×900 with large fonts.

## Prompt for your coding agent
> Read `AGENTS.md`, `docs/BRIEF.md`, `docs/CONTRACT.md` (§9, §10), `docs/handoff/DEV3-frontend-zhaoqinghe.md` and `fixtures/api/README.md` fully. You are DEV3 (frontend) on branch `feat/frontend-zhaoqinghe`. Work only inside `frontend/`. Scaffold Vite + React + TypeScript + Mantine + TanStack Query. Build screens 1 and 2 against the mock fixtures in `fixtures/api/`, with a mock-mode switch in `src/api.ts` and a fake SSE replay of `fixtures/api/stream.ndjson`. Type the API from the fixture shapes. Commit after each screen and push to `feat/frontend-zhaoqinghe`. Report what you ran and what you didn't.

## Round 2 (added Fri 21:30) — after screens 1–6
No new top-level pages; extend the existing ones (APIs in CONTRACT §9, §10):
- **R1 Onboarding → "Verify with samples"** (headline innovation):
  - two boxes, "Should notify" and "Should stay quiet", plus a transform editor (scale, unit) per numeric field;
  - optional "expected recipients / channels" per sample;
  - Run → one card per sample: pass/fail, expected vs actual recipients and channels, **the field that decided it**, unit warnings;
  - a short line in plain English, e.g. "notional is 25,000,000 in cents = 250,000 EUR → below the 10,000,000 threshold → quiet".
- **R2 Rule editor → "Safety check" panel** (`POST /v1/rules/check`): the scenarios checked (a compact table), and the counterexamples in red with a one-line reason and a fix hint.
- **R3 States in existing screens:**
  - "Delivery at risk" badge in the header (from `stats.tick.at_risk`);
  - "Format changed" (schema drift) on the onboarding list;
  - runbook step "Result unknown — check" (action state `unknown`) with a **Check result** button;
  - timeline entries "Old recovery ignored" (`STALE_RESOLVE`) and "Event quarantined" (`QUARANTINED`).
- **R4 SSE:** handle coalesced `alert.updated`; refetch the open detail after a reconnect.
- **Tier 2 UI** ([IMPROVEMENTS.md](../IMPROVEMENTS.md)): #1 silences (create from an incident: matcher prefilled, expiry; show "Silenced by X until T"), #2 escalation steps in the timeline, #14 a "No action needed" resolve reason and the resulting proposal card.
- **P2:** an Investigation panel on incident detail (tool, time, evidence, unknowns; label simulated data as simulated).

## Full target list (all required, in order) — updated Fri 22:00
**Please push work in progress to `feat/frontend-zhaoqinghe` now and every hour.** Yi's backend is already on `main`, so screens 1–3 can switch to the real API (`/v1/alerts`, `/v1/alerts/{id}`, notifications, runbook, ack/resolve) as soon as you're ready.

**Screens**
- ☐ 1 live feed
- ☐ 2 incident detail + explain
- ☐ 3 runbook panel + approvals
- ☐ 3b Propose fix
- ☐ 4 rules editor
- ☐ 5 people/preferences
- ☐ 6 onboarding
- ☐ 7 impact preview + proposals
- ☐ 8 dashboard
- ☐ 9 NL rule box

**Round 2**
- ☐ R1 "Verify with samples" (expected vs actual)
- ☐ R2 Safety check panel
- ☐ R3 states: delivery at risk, format changed, result unknown, old recovery ignored, quarantined
- ☐ R4 SSE coalescing + refetch on reconnect

**Tier 2**
- ☐ #1 silences
- ☐ #2 escalation steps in the timeline
- ☐ #14 "No action needed" + proposal card

**Tier 3**
- ☐ #17 investigation panel
- ☐ #18 shadow-rule diff view
- ☐ #19 SLO / error-budget view
- ☐ #20 "similar past incident" hint card
- ☐ #16 cost-ladder steps in the timeline

Label simulated data as simulated.

## Round 3 (Sat 01:00) — mentor feedback; PRs open by 06:00
First merge the latest `main` into your branch and keep main's real-API screens (`SchemaOnboarding.tsx`, `RunbookCatalogue.tsx`); the mock-only verification panel must not ship. Then, in [IMPROVEMENTS.md](../IMPROVEMENTS.md) Round 3 order: **M1** approval buttons use the API's `run_id` and the home count reads the real number, **M15** screens, **M13** response-metric tiles, **M10** restore button. The mentor said the UX will be fine; spend the time on working flows, not visual effects.

### Round 3b (Sat 02:45)
- Your branch is about 147 commits behind `main`. **Merge `main` before anything else.**
- **New: M19** live funnel and outcome tiles, and the Approvals overview from the real pending list.
