# Alert Funnel frontend prototype

Work only on `feat/frontend-zhaoqinghe`. Vite + React + TypeScript + Mantine 7 + TanStack Query. All mock fixtures/config are read-only imports from the repository.

## Run

```sh
cd frontend
npm install
npm run dev
```

Preview: http://127.0.0.1:5173/. Hot updates are enabled. `VITE_MOCK=1` is the default. For API mode, start with `VITE_MOCK=0`; optionally set `VITE_API_BASE=http://localhost:8000` (without it Vite proxies `/v1` to port 8000). No silent fallback to mocks. Refreshing restarts the synthetic incident scenario; rules, preferences and confirmed mapping persist in this browser. Mock event/action state is session-local.

## Navigation

- `/`: overview, current active alerts/critical count, pending action approvals, delivery risks, workflow grouping scenario, recent actions, functional shortcuts.
- `/alerts`: original live feed with filters.
- `/a/:id`: detail, acknowledgement/resolution, delivery attempts, timeline and runbook.
- `/approvals`: shared mock runbook approval inbox. Configuration proposals are separate.
- `/rules`, `/people`, `/runbooks`, `/proposals`: configuration navigation.
- `/onboarding`: mapping review and local positive/negative scenario verification.
- `/dashboard`: detailed operational snapshot. Critical is separate from the processing stages.

Header identity is **simulated auth**. User mode limits personal preference edits and ownership/team actions. Admin mode is mock-only; it does not add backend privileges. File-origin rules remain read-only even in admin mode.

## Honest simulation boundaries

Mock SSE replays repository events, introduces settlement incident #101, increments to the fixture's 500 occurrences, and seeds a demo approval. Safe actions and approvals are simulated; they do not contact external services. Activity/timeline/overview updates share session state. No external delivery occurs.

Natural-language drafting and AI mapping/structuring use labelled preset suggestions, not an LLM. The rule editor accepts arbitrary field paths with suggestions derived from fixture events. The safety checker checks explicit positive/negative samples, not exhaustive correctness. Local impact checks compare sample matching and recipient/channel choices; they do not implement the backend's grouping/cooldown/throttle engine. Preference resolution in these checks uses a labelled demo clock of 12:00. Unit/scale warnings require human review.

Approving a mock proposal can save a UI rule. File rules are never overwritten: a proposed copy remains separate and can therefore cause duplicate matches if both rules are enabled. Review before applying.

## API gaps in this checkout

- Synced the same frontend branch to `701304d`; `docs/IMPROVEMENTS.md`, Round 2 and full target list are now present. No separate branch was merged. Higher-tier work remains gated by integrated core-chain acceptance.
- Incident/detail/notifications/actions backend code is present; local API connectivity, integration and actual routing/delivery remain NOT_RUN. Config/SSE routers are still in progress. `VITE_ALERTS_MODE=api` can select real core reads while keeping config mocked.
- There is no defined global action-approval list endpoint or global activity endpoint. Real actions remain accessible from individual incident runbooks.
- Safety `/rules/check` and verification `/schemas/verify` are now specified; safety API request is wired, schema verification is still local. Proposal response shapes remain unspecified. Local simulations add no new backend endpoints.
- Funnel fixtures expose oldest pending processing, not oldest pending delivery or total retry attempts. Missing measurements are not inferred.
- The chart now records actual local replay updates (cumulative incident occurrences, not measured throughput). Replay / pause / speed and sample inspection are interactive, respect reduced motion, and never poll the feed.

See VALIDATION.md for checks and remaining integration work. Commits and pushes only when explicitly requested.

## Visual/interaction update

Active alerts use a severity-sorted lead incident plus a compact secondary queue. No user avatars are displayed. Summary values are unboxed. Replay resets only the synthetic workflow scenario (with confirmation when actions exist), preserves rules/preferences, and supports pause/resume/speed. The trend is derived from replay history and has a keyboard-accessible update scrubber.

Known API mismatch: rendered pending steps have a run count/state but no action-run ID in the current fixture/backend renderer. Existing API approvals cannot be safely submitted until that ID is supplied. The UI disables those submissions; freshly requested runs can use their returned IDs. Check result refetches the runbook and never reruns an unknown action.
