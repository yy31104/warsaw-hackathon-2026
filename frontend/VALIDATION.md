# Mock redesign validation

2026-11-20 · frontend only · real backend NOT_RUN

| Check | Result | Evidence / scope |
|---|---|---|
| Production build | PASS | npm run build; bundle-size advisory remains |
| Lint | PASS | npm run lint |
| Logic tests | PASS | 9 tests: partial SSE updates, filters, ownership, action approval/timeline, independent identities, arbitrary field flattening and lost-notification comparison |
| Horizontal navigation / overview | PASS | Browser inspected at 1440×900; small-width layout also observed |
| Live incident / counter | PASS | Browser observed #101 enter with 1 occurrence, later 500 |
| Explain notification reason | NOT_RUN | Existing fixture explanation preserved; actual backend per-condition trace not verified |
| Safe run, approver switch, approve, resolve | PASS | Browser: sam ran step/acknowledged; lee approved and resolved; shared audit timeline updated |
| Explicit request-approval button | NOT_RUN | Request state/authorization covered by logic test; browser flow used SSE-seeded pending request |
| Anna SMS 09:00–18:00 | PASS | Browser filled field and observed saved confirmation |
| Create/test FX trade rejection rule | PASS | Browser saved fx-desk-preview, matched sample true; recipient anna/sms shown |
| Sample impact preview | PASS | Browser observed before/after recipient table; limited local simulation |
| Unknown schema review/verification/confirmation | PASS | Browser observed Positive and Quiet PASS; switched ops and confirmed mapping |
| Proposal approval and rule publication | NOT_RUN | Implemented mock workflow; browser not exercised in this pass |
| Overview/timeline linkage | PASS | Resolved incident removed from active count; recent activity showed run, approval, resolution and rule save |
| Dark/light visual regression across every screen | NOT_RUN | Prior themes checked; redesigned desktop light layout inspected, full suite pending |
| Real API, delivery, reprocessing, LLM | NOT_RUN | No running complete backend; no claim of backend verification |

## Remaining work

- Full browser coverage for proposals, explicit approval request, all runbook formats, negative/error/reconnect cases, and dark/light visual checks on all pages.
- Replace preset natural-language/AI suggestions with real APIs once available; validate response shapes with owners.
- Rule test UI still uses a compact local result; expand trace presentation and stronger draft validation.
- Schema verification is an explicit local test rule, not the ingest/normalization pipeline. Time-to-first-real-routed-alert remains NOT_RUN.
- Complete runbook catalogue management; current configuration shortcut opens the working incident runbook/format explorer.
- Refine the large frontend components and consolidate historical CSS layers after visual feedback stabilizes.

## Latest same-branch sync and interactive alert layout

- Synced to 701304d; build/lint PASS after updated notifications/runbook fixtures.
- PASS: browser active severity filter (Warnings/All), replay confirmation and immediate pause; pending counts reset, configuration preserved.
- PASS: severity sorting test prevents a high-score warning from displacing a more severe error.
- NOT_RUN: real API connectivity; localhost:8000 did not return a running health response. No Docker executable found in this environment.
- NOT_RUN: time-series inspection slider and speed timing assertions; controls implemented, timing not measured.
- Tier 2/3 integration remains gated by latest handoff: core chain must first PASS on integrated main.

## Pre-push checks, 22:23 Europe/Warsaw

- PASS: frontend build, lint and 9 logic tests.
- PASS: make test using locked dependencies in a temporary environment: 59 passed, 22 skipped. Database-dependent tests skipped because PostgreSQL was unavailable; they are NOT_RUN, not integration PASS.
- Status summary redesigned as a severity ring plus staggered Critical, approval and risk controls, animated values and reduced-motion support.

## Activity stream follow-up

- PASS: frontend 10 logic tests including generated occurrence → count/activity linkage.
- PASS: browser observed post-replay counts 501, 502, 503 and corresponding pushed activity entries; Pause view freezes displayed entries while the stream continues.
- Redundant Next actions and Review approvals shortcuts removed.

## Spacing and baseline follow-up

- PASS: browser geometry confirms Active alerts and Recent activity section tops and heading tops match exactly.
- Demo controls collapsed by default; longer grouping explanation is on demand; repeated copy removed and section/list spacing increased.
- Frontend build/lint PASS; 10 logic tests PASS; make test 59 passed / 22 skipped (PostgreSQL integration NOT_RUN).
