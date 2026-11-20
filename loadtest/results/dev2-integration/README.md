# DEV2 local integration acceptance

For the feature branch's pre-merge run against the updated main baseline, see
the [Golden Runner gate record](../../../docs/GATES.md#golden-run). This report
keeps the earlier browser, throughput and outage measurements with their original commits.

The integration branch is `local/dev2-control`. It starts at data commit `18951ac` and includes the real frontend at `49e55ba`, Yi's routing and verification from PR #10 at `962bad5`, and the golden runner and mock operations service from PR #11 at `09c51d4`. The tests use synthetic events. The phone simulator receives SMS and calls, and Mailpit receives email.

The initial full Chromium run used application commit `8e4d5c1`. The integration controller independently repeated all five browser scenarios on the release code at `f5513f4` and all five passed. The worker and outage measurements used `29b0dce`, before the notification link was configured to use the isolated frontend port. [Release browser JSON and screenshots](release-browser.zip)

## Acceptance results

| Requested feature | Status | What we observed |
|---|---|---|
| Live alerts in the browser and simulated phone | PASS | One settlement timeout on RabbitMQ produced one SMS and one call for Sam. A TIMEOUT card appeared in Settlements without a page refresh. Chromium opened the incident directly from the URL shown in the SMS on the isolated frontend. Switching to Pat selected Pricing automatically. Its list and event stream contained no settlement alert, including after reload. |
| Save configuration and apply it immediately | PASS | Rule v1 sent Sam an SMS. After a browser save, the next matching event used v2 and called Lee. Anna changed her own channel from SMS to phone. Her change survived a reload and the next event used it. Database tests also cover teams, runbooks and proposals. |
| Verify unfamiliar JSON | PASS | The browser showed recipients, channels and deciding fields for positive and quiet examples. Publishing the mapping replayed the retained event. A separate integration test compared actual notification rows for two trade amounts after historical replay and fresh ingestion. |
| Tail logs and query event history | PASS | The tailer handled append, rotation, truncation and restart. The HTTP API filtered records by source, schema, status and field. Yi's golden runner paged through 205 unique records. |
| Draft a runbook or rule and preview its impact | PASS | The real UI structured text, obtained a rule draft, ran Yi's safety check and showed which recipients would lose notifications. Database tests verified that previews left alerts, notifications, delivery attempts and configuration unchanged. |
| Start, measure and recover the stack | PASS | Isolated Compose projects ran the same workload with one and two normal workers. Receiver-side measurements and both physical outage trials are below. |

- [Browser commands, screenshots and Chromium records](../web-g1/README.md)
- **PASS** for the Linux backend suite. It ran 380 passing tests and skipped none. One existing test is marked as an expected failure for the isolation of synthetic canary events from regular rules. [Backend test output](backend-pytest.txt)
- **PASS** for the 10 frontend tests, ESLint and the real-API production build. [Frontend output](frontend-checks.txt)
- The earlier Golden run had **31 PASS, 1 FAIL, 0 NOT_RUN**. In `inbound-approval-brute-force-locked`, the sixth attempt received HTTP 429, but the alert timeline omitted the sender's lock record. [Earlier case results](golden.md)

## Implementation and ownership

| Worktree | Changes |
|---|---|
| `dev2-web` | Real frontend, team-specific feed and SSE, browser editing, regression harness |
| `dev2-config` | Persistence for rules, teams, personal preferences, runbooks and proposals, version conflicts and hot reload. Migration 202 |
| `dev2-schema` | Shared field mapping, unit conversion, Yi's verifier and replay after mapping approval |
| `dev2-inputs` | Continuous log tailing and transactional checkpoints, filtered history with cursor and offset pagination. Migration 215 |
| `dev2-preview` | Draft generation, Yi's rule checker and recipient differences across a shared sample of historical events |
| `dev2-perf` | Compose, metrics, reproducible load generation, collection and outage reconciliation |

Workers read the committed configuration revision when they claim the next batch. File-owned rules and teams are read-only in the browser. The browser exercise edits a UI-owned rule, and personal preferences are stored separately from each user's file-owned identity and contact details.

The log tailer starts at the beginning of a new file. If rotation happens while it is stopped, retain the renamed file in the same directory until its remaining lines are read. A truncate-and-regrow operation that restores identical bytes at the sampled checkpoint positions between polls is undetectable by this reader.

## Throughput, latency and losses

Both runs used the same 14,040 RabbitMQ events over 20 seconds. The generator offered 700 normal and two critical events per second. The normal events shared one alert key. Each run produced 82 distinct SMS and phone receipts. Latency starts at the generator send timestamp and ends at the simulator's receipt timestamp. The controller independently recomputed both percentiles from the compressed raw records by matching notification IDs.

| Normal workers | Ingested events/s | Processed events/s | Total processed | Receipts | p50 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 702.00 | 699.33 | 14,040 | 82 | 236 ms | 435 ms |
| 2 | 702.00 | 700.12 | 14,040 | 82 | 234 ms | 376 ms |

The critical worker and delivery worker each stayed at one replica. The generator capped the input rate. These numbers describe that offered load. Browser acceptance used project `dev2web` on ports 181xx. The benchmarks used ports 186xx and fresh PostgreSQL, RabbitMQ and simulated phone storage for each trial. [Machine, benchmark results and raw records](../platform-bench/README.md)

The separate k6 HTTP test offered 20 requests/s for five seconds. All 100 requests were accepted and all 100 unique IDs were found in processed events. k6 recorded zero dropped iterations. HTTP request duration was 11.48 ms at p50 and 20.61 ms at p99. [HTTP results, event ledger and replay command](../http-k6-smoke/README.md)

| Fault | Observed recovery |
|---|---|
| Normal ERROR alerts, container stopped for 30.020 s | Twelve events produced 24 original SMS and phone notifications. The receiver recorded zero while stopped. After restarting the same container with its existing volume, it held all 24 original IDs. Loss was zero. |
| Critical alerts, container stopped for 30.016 s | Twelve events produced 24 original notifications. All 24 IDs appeared at the receiver after recovery. Loss was zero. |
| Critical alerts, earlier HTTP 503 rehearsal | The phone recorded zero of 24 original IDs. The fallback policy created 24 replacements. PostgreSQL recorded 12 UI deliveries and 12 SMTP acknowledgements. Independent Mailpit readback for this rehearsal was not run. |

## Reproduce

Run these commands in PowerShell at the repository root. The backend suite creates temporary PostgreSQL and RabbitMQ instances. The golden runner creates and removes its own Compose project and volumes. The Pricing digest case waits for its actual five-minute interval.

```powershell
.\tools\test_isolated.ps1
.\tools\test_golden_isolated.ps1
```

To run frontend checks and build with the real API enabled:

```powershell
Push-Location frontend
npm ci
npm test
npm run lint
$env:VITE_MOCK = '0'
$env:VITE_ALERTS_MODE = 'api'
npm run build
Pop-Location
```

To repeat both worker counts and the two physical outage trials:

```powershell
.\loadtest\run-platform-bench.ps1 -Trials one-normal,two-normal,telecom-outage,critical-outage
```

Demo actors and real local network transports were enabled. Model-assisted paths were tested with offline heuristics and controlled model responses. Live external model calls and the judges' private test data are **NOT_RUN**.
