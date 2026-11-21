# Console validation

The browser run used the production `VITE_MOCK=0` frontend on a fresh Compose project with PostgreSQL, RabbitMQ, both ingest replicas, the rule and delivery workers, fake telecom, Mailpit and mock ops. Assist was disabled and no provider keys were set. Inputs and actors were synthetic. [Test results and API request counts](evidence/console/result.json)

## Checks

| Command | Result |
|---|---|
| `npm run build` | PASS, also built inside Compose |
| `npm run lint` | PASS |
| `npm test` | PASS, 16 tests |
| `make test` | PASS, 580 tests with PostgreSQL and RabbitMQ |
| `npm run test:console` | PASS, 10 browser checks |
| Independent novice trial | NOT_RUN. The requested self-test is the scripted browser run below. |

| Browser check | Result | Elapsed |
|---|---|---:|
| A · live critical incident and acknowledge | PASS | 1.30 s |
| B · Lee approves persisted retry in a separate context | PASS | 0.55 s |
| C and F · unknown JSON verified and routed with assist off | PASS | 0.95 s |
| D · rule typo suggestion and versioned publish | PASS | 0.38 s |
| E · structured runbook validation blocks publish until fixed | PASS | 0.27 s |
| On call · preference sliders persist | PASS | 0.15 s |
| Health · live counters and dated benchmark provenance | PASS | 0.09 s |
| Intro · auto-enter and session replay | PASS | 6.62 s |
| Screenshots · four layouts across eight views | PASS | 4.47 s |
| Network · genuine API, no fixtures or browser errors | PASS | 0.01 s |

The open incident arrived live and its count rose to 2. Sam's fake provider independently recorded one call and one SMS. Acknowledging set `next_escalation` to `null`. Lee reloaded a separate browser context, approved run 1, and Sam saw Lee's name and the server's `succeeded` state. The incident status read back as resolved.

The unfamiliar JSON test started in `needs_review`. The unconfirmed draft failed verification. After setting the scale and severity and narrowing the detection fields, both examples passed. The publish response rechecked 1 waiting event and the next event routed to Rita by email. The verified transformed amount was 2,500,000.

The browser made no fixture or external requests and logged no uncaught page errors. On this base, Why you stays hidden because alert detail omits `representative_event_id`. The alert ID and event ID in the test were 3 and 8. The unit test covers the explain path when the representative ID is provided.

## Friction observed and corrected

- During onboarding, filled textareas and the priority selector needed explicit accessible names. The retest located the controls by their labels. The page lists the eligible reviewing actor next to Go live.
- The initial suggested detector covered the priority field alone. The format panel now offers Match sample shape. The test asserts the actual reprocessed count from the server.
- At 390 px, the old definition-list width pushed the last funnel label past the viewport. The final screenshots assert the document width and the fixed bottom navigation on every phone view.

## Screenshots

Desktop uses a 1440 × 900 viewport and full-page captures outside the intro. Phone uses a 390 × 844 viewport. Both themes came from the same production build. The Intro captures use reduced motion.

| View | Desktop light | Desktop dark | Phone light | Phone dark |
|---|---|---|---|---|
| Now | [desktop light](evidence/console/desktop-light/now.jpg) | [desktop dark](evidence/console/desktop-dark/now.jpg) | [phone light](evidence/console/phone-light/now.jpg) | [phone dark](evidence/console/phone-dark/now.jpg) |
| Incident | [desktop light](evidence/console/desktop-light/incident.jpg) | [desktop dark](evidence/console/desktop-dark/incident.jpg) | [phone light](evidence/console/phone-light/incident.jpg) | [phone dark](evidence/console/phone-dark/incident.jpg) |
| Sources | [desktop light](evidence/console/desktop-light/sources.jpg) | [desktop dark](evidence/console/desktop-dark/sources.jpg) | [phone light](evidence/console/phone-light/sources.jpg) | [phone dark](evidence/console/phone-dark/sources.jpg) |
| Rules | [desktop light](evidence/console/desktop-light/rules.jpg) | [desktop dark](evidence/console/desktop-dark/rules.jpg) | [phone light](evidence/console/phone-light/rules.jpg) | [phone dark](evidence/console/phone-dark/rules.jpg) |
| Runbooks | [desktop light](evidence/console/desktop-light/runbooks.jpg) | [desktop dark](evidence/console/desktop-dark/runbooks.jpg) | [phone light](evidence/console/phone-light/runbooks.jpg) | [phone dark](evidence/console/phone-dark/runbooks.jpg) |
| On call | [desktop light](evidence/console/desktop-light/on-call.jpg) | [desktop dark](evidence/console/desktop-dark/on-call.jpg) | [phone light](evidence/console/phone-light/on-call.jpg) | [phone dark](evidence/console/phone-dark/on-call.jpg) |
| Health | [desktop light](evidence/console/desktop-light/health.jpg) | [desktop dark](evidence/console/desktop-dark/health.jpg) | [phone light](evidence/console/phone-light/health.jpg) | [phone dark](evidence/console/phone-dark/health.jpg) |
| Intro | [desktop light](evidence/console/desktop-light/intro.jpg) | [desktop dark](evidence/console/desktop-dark/intro.jpg) | [phone light](evidence/console/phone-light/intro.jpg) | [phone dark](evidence/console/phone-dark/intro.jpg) |

## Reproduce

From the repository root, start a fresh disposable project on available ports. The browser test requires empty Settlements runtime state because it checks the first live incident.

```sh
export API_PORT=43800 FRONTEND_PORT=43873 POSTGRES_PORT=43832
export RABBITMQ_PORT=43872 RABBITMQ_MANAGEMENT_PORT=43871
export MAILPIT_PORT=43825 SMTP_PORT=43826 TELECOM_PORT=43810 MOCK_OPS_PORT=43820
export DEMO_MODE=1 ASSIST_ENABLED=0 OPENAI_API_KEY='' ANTHROPIC_API_KEY=''
docker compose -p alertconsole-qa up -d --build --wait
cd frontend
npm ci
npx playwright install chromium
export CONSOLE_TEST_UI=http://127.0.0.1:43873
export CONSOLE_TEST_API=http://127.0.0.1:43800
export CONSOLE_TEST_TELECOM=http://127.0.0.1:43810
export CONSOLE_TEST_OUTPUT=../.console-test-output
npm run test:console
```

See the [API plan](../docs/FRONTEND_CONSOLE_PLAN.md) for missing backend fields.
