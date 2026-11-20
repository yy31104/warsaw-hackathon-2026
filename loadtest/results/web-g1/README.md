# Live browser acceptance

The 20 November 2026 run passed in Chromium 149.0.7827.55 on the clean integrated source `4646e34c68b5ca7f94feb37e9c5443204fa8ec12`. All 13 Compose services retained their container IDs, start times and restart counts throughout the run. [Browser assertions and raw IDs](browser-g1.json) · [Compose snapshots](runtime.json)

| Check | Result | Observed behavior |
| --- | --- | --- |
| Settlement timeout | PASS | RabbitMQ event 8 produced alert 3. It arrived in the Settlements feed without navigation. Sam received exactly one SMS and one phone call in the simulator. [Settlements feed](settlements-timeout.png) · [Sam's phone](sam-phone.png) |
| Team-scoped feed | PASS | Choosing Pat automatically selected Pricing. The Pricing tab received zero Settlements stream events. The refreshed tab and the tab switched from Sam to Pat requested Pricing lists and displayed their Pricing alerts. [Pricing after reload](pricing-reloaded.png) |
| Rule edited in the UI | PASS | Version 1 delivered event 9 to Sam by SMS. The first event after saving v2, event 10, delivered to Lee by phone. Event 11 reused the old condition and recorded `NO_MATCH` and zero notifications. [Saved rule](rule-version-2.png) |
| Personal preferences | PASS | At the same demo clock, Anna's critical alert switched from SMS to phone on the first event after saving. Reloading the preferences page showed the saved phone setting. [Saved preferences](anna-preferences-reloaded.png) |
| Unknown format | PASS | The verification UI passed the positive and quiet examples. Confirming the mapping requeued one stored event with its original ID. The next positive event delivered to Ops in the UI, and the next quiet event produced zero notifications. [Verification and publication](schema-verify-and-publish.png) |
| Drafts and proposal impact | PASS | With `ASSIST_ENABLED=0`, the heuristic endpoints produced the editable rule and runbook drafts. The reviewed runbook retained the restart approval requirement and saved its content. A proposal preview replayed 16 events, identified the event that would lose its phone notification, and left v2 active until Sam approved v3. [Runbook save](runbook-saved.png) · [Impact preview](proposal-impact.png) |

## Reproduce from PowerShell

Run the harness from the integrated repository root with Docker Compose, Node.js, and `uv`. The Compose frontend compiles with `VITE_MOCK=0` and `VITE_ALERTS_MODE=api`. Its nginx server proxies `/v1` to the real API. The harness drives Chromium and publishes the original settlement fixture through RabbitMQ.

The recipe uses a disposable `dev2web` project with its own PostgreSQL, broker, telecom inbox, and mocked ops volumes. Start with fresh volumes so the settlement alert and Anna's changed preferences from a previous run cannot affect the checks. The events, contacts, and credentials are synthetic demo data.

```powershell
npm --prefix frontend ci
npm --prefix frontend exec -- playwright install chromium
uv sync --project backend --locked

$env:COMPOSE_PROJECT_NAME = 'dev2web'
$env:POSTGRES_PORT = '18132'
$env:RABBITMQ_PORT = '18172'
$env:RABBITMQ_MANAGEMENT_PORT = '18174'
$env:SMTP_PORT = '18125'
$env:MAILPIT_PORT = '18126'
$env:TELECOM_PORT = '18110'
$env:MOCK_OPS_PORT = '18120'
$env:API_PORT = '18100'
$env:FRONTEND_PORT = '18173'
$env:ASSIST_ENABLED = '0'
$env:DEMO_MODE = '1'
docker compose -p dev2web up -d --build --wait --wait-timeout 100

$env:WEB_G1_FRONTEND_URL = 'http://127.0.0.1:18173'
$env:ALERTING_API_URL = 'http://127.0.0.1:18100'
$env:FAKE_TELECOM_URL = 'http://127.0.0.1:18110'
$env:RABBITMQ_URL = 'amqp://guest:guest@127.0.0.1:18172/'
node tools/browser_g1.mjs
```

The script writes JSON and screenshots to `loadtest/results/tmp/web-g1` by default. Override the destination with `WEB_G1_ARTIFACTS`. Pass `--g1-only` for settlement delivery and browser team isolation. Pass `--workflows-only` for the real configuration flows. Set `WEB_G1_PYTHON` only when reusing an existing Python environment with the backend dependencies.

Clean up the disposable project after collecting the evidence.

```powershell
docker compose -p dev2web down --volumes
```
