# Alert Funnel console

The React and TypeScript console opens on Now. It uses the existing FastAPI endpoints and team-scoped event stream. See the [screen and API plan](../docs/FRONTEND_CONSOLE_PLAN.md) and [browser validation](CONSOLE_VALIDATION.md).

## Run

Run `docker compose up -d --build` from the repository root and open http://127.0.0.1:5173/. Compose builds the frontend with `VITE_MOCK=0`. The same-origin proxy serves the API and SSE. Select a demo identity under **Viewing as**. The project uses simulated sign-in with `X-User`.

For frontend development with the existing API running on port 8000, run the following in `frontend/`.

```sh
npm ci
npm run dev
npm run build
npm run lint
npm test
```

Set `VITE_API_BASE` for a different API origin. The local fixture workspace is available by explicitly starting Vite with `VITE_MOCK=1`. Its incident replay is local and simulated. `VITE_ALERTS_MODE=api` can connect the fixture workspace to the core incident tools during development.

## Routes

| Route | Task |
|---|---|
| `/` | Review urgent incidents and approvals. |
| `/a/:id` | Acknowledge, resolve and run rendered steps. |
| `/sources` | Map an unfamiliar event, verify two samples and publish. |
| `/rules` | Edit and check routing, preview impact and publish. The Proposals tab links to the existing review workflow. |
| `/runbooks` | Structure text, fix invalid steps and publish. |
| `/oncall` | See the current roster and edit your notification window. |
| `/health` | Inspect live pipeline counters and dated benchmark evidence. |

The existing incident search, approval inbox, detailed delivery, advanced mapping, rule editor, runbook catalogue and preference editor remain available from the console. The Intro button replays the labelled example.

## Browser checks

Use a dedicated Compose project with disposable volumes. Set `CONSOLE_TEST_UI`, `CONSOLE_TEST_API` and `CONSOLE_TEST_TELECOM` to that project's loopback URLs, then run `npm run test:console` in `frontend/`. The suite publishes synthetic configuration and events to the target project. Use `CONSOLE_TEST_OUTPUT` to select the evidence directory.
