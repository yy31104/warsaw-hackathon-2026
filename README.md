# Alert Funnel

Alert Funnel is a flexible alerting and notification service built for the 2026 Goldman Sachs Warsaw Hackathon. It turns workflow failures, webhooks and application logs into incidents for the people on call. Teams can register an unfamiliar schema and verify who will be notified before publishing. Retransmissions are deduplicated and repeated failures update one incident with a counter. The live console shows delivery attempts and approval-gated runbook steps. Each event has an explanation of the rules and routing decisions it triggered.

## Quick start

You need repository read access, Git, Docker with Compose, and curl. The demo runs with the default Compose settings and requires no provider key.

```sh
git clone --branch main https://github.com/yy31104/warsaw-hackathon-2026.git
cd warsaw-hackathon-2026
docker compose up -d --build --wait

curl --fail-with-body -sS http://127.0.0.1:8000/v1/events/generic-webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: demo-generic-key' \
  -H 'X-Demo-Clock: 2026-10-23T11:15:00+02:00' \
  --data-binary @fixtures/events/alertmanager_high_error_rate.json
```

The demo clock selects Anna’s daytime SMS preference in Warsaw. Open fake-telecom and select Anna’s number, or paste the numeric ID returned in `event_ids` at the `read` prompt below. After processing, the explanation shows a `NEW_ALERT` decision for `anna-payments-critical` and Anna’s SMS delivery. [Simulated phone](http://127.0.0.1:8100/) · [Operator console](http://127.0.0.1:5173/) · [Mailpit](http://127.0.0.1:8025/) · [Sample payload](fixtures/events/alertmanager_high_error_rate.json)

```sh
read -r event_id
curl -fsS "http://127.0.0.1:8000/v1/events/${event_id}/explain"
```

**Fresh clone to first alert** — TBD by W0. [Acceptance gate](docs/GATES.md#g3--feature-freeze-sat-0730)

The inputs share a durable PostgreSQL inbox. Rule workers claim work in separate normal and critical lanes and write decisions, alert updates and the notification outbox in one transaction. Delivery workers claim outbox entries and retry independently of intake. The console receives committed changes over server-sent events (SSE). [Scaling and shared state](docs/SCALING.md#what-another-replica-can-do)

## Task requirements and tests

The golden IDs refer to the committed black-box expectations. Run the golden runner on a disposable demo stack because it resets demo state between cases. The last column also links focused tests for behavior the golden cases only cover in part. [Golden cases](fixtures/golden/expectations.yaml) · [Runner](loadtest/golden_runner.py) · [Isolated Compose runner](tools/test_golden_isolated.ps1)

| Task | Feature | Golden case IDs | Focused tests |
| --- | --- | --- | --- |
| 2.1 Sources and schemas | RabbitMQ, JSON webhook and tailed log ingestion. The registry maps different event shapes. Unmatched schemas go to review. | `settlements-positive`, `rates-positive`, `pricing-digest`, `unknown-schema-visible`, `verify-unit-change` | [RabbitMQ E2E](backend/tests/processing/test_broker_e2e.py), [HTTP](backend/tests/ingest/test_http.py), [log input](backend/tests/ingest/test_logfile.py), [schema verification](backend/tests/ingest/test_verify_schema.py) |
| 2.2 Team rules | Nested field filters with AND/OR, per-rule routing and title/body/field/color templates, and runtime publishing. | `settlements-negative-env`, `settlements-negative-workflow`, `rates-boundary-equal`, `rates-schema-glob-v3`, `rates-missing-template-field`, `rule-safety-counterexample` | [Golden rule evaluation](backend/tests/processing/test_golden.py), [DSL](backend/tests/processing/test_dsl.py), [runtime publishing](backend/tests/ingest/test_config_runtime.py) |
| 2.3 Preferred delivery | Email, SMS, phone and UI routes. Preferences can vary by severity and local time. The outbox retains notifications for retry on channel failure. | `settlements-positive`, `rates-positive`, `anna-daytime-sms`, `anna-evening-email`, `anna-boundary-0800`, `anna-boundary-2000`, `channel-down-no-loss` | [Routing](backend/tests/processing/test_routing.py), [delivery policy](backend/tests/processing/test_delivery_policy.py), [email adapter](backend/tests/delivery/test_email.py) |
| 2.4 Service UI | Live team feed, rule-defined presentation, acknowledge and resolve, rule and preference editing, and proposals from incident detail. | `settlements-positive` (presentation), `rates-positive` (UI route), `alertmanager-auto-resolve` (lifecycle) | [SSE](backend/tests/ingest/test_stream.py), [feed and audit](backend/tests/processing/test_api.py), [config and proposals](backend/tests/ingest/test_config_runtime.py), [browser scenarios](frontend/e2e/console.mjs) |
| 2.5 Control noise | Retransmissions collapse under the same source identity. Real occurrences update the incident counter, with per-recipient throttling and rule-level digests. | `retransmission-dedup`, `storm-500-one-alert`, `pricing-digest` | [Inbox deduplication](backend/tests/ingest/test_store.py), [aggregation](backend/tests/processing/test_worker.py), [throttling](backend/tests/processing/test_delivery_policy.py) |
| 2.6 Runbooks | Structured YAML, Markdown, checklists, text and links. Event values fill steps and notification excerpts. Risky actions go through approval and the audit history records the actor and outcome. | `settlements-runbook-rendered`, `approval-required`, `approval-wrong-person`, `approval-ok`, `alertmanager-auto-resolve` | [Runbook rendering](backend/tests/processing/test_runbooks.py), [channel excerpts](backend/tests/processing/test_runbook_excerpts.py), [approvals](backend/tests/processing/test_approvals.py) |

## Measured results

The runs below use simulated receivers. The offer rate is generated traffic, and the reported processing rates use the measurement windows identified in each row. Receiver percentiles include verified receipts through the stated cutoff. [Scaling report and methodology](docs/SCALING.md) · [Load-test driver](loadtest/run_saturation.py)

| Machine | Load and configuration | Observed result | Source files |
| --- | --- | --- | --- |
| Intel Core Ultra 9 275HX, Docker Desktop on WSL2 | 1,400 normal + 2 critical events/s, 1,000 normal keys, 60 s offer, one intake and one delivery worker | **626.0 → 1,170.0 → 1,405.9 sustained processed events/s** after warm-up with one, two and four normal rule workers | [Machine](loadtest/results/saturation-ae69a28/machine.json), [matrix](loadtest/results/saturation-ae69a28/runs.csv) |
| Intel Core Ultra 9 275HX, Docker Desktop on WSL2 | 2,800 normal + 2 critical events/s, 1,000 normal keys, 60 s offer, one intake and four normal rule workers | Critical receiver **p99 0.443 s** across 240 receipts. The normal rule inbox grew to a late median of 56,040.5 pending events. | [Machine](loadtest/results/saturation-ae69a28/machine.json), [trial](loadtest/results/saturation-ae69a28/m11-11210120-1d623-r2800-k1000-w4/summary.json) |
| Intel Core Ultra 9 275HX, Docker Desktop on WSL2 | 5,600 normal + 2 critical events/s, one hot normal key, 60 s offer, four normal rule workers, one vs. two intakes | **3,571.0 → 5,546.8 ingested events/s**. Observed critical receiver p99 moved from 28.449 s (206/240 receipts) to 1.233 s (238/240). | [Machine](loadtest/results/saturation-ae69a28/machine.json), [one intake](loadtest/results/saturation-ae69a28/diagnostic-intake1/m11-11210200-9f20c-r5600-k1-w4/summary.json), [two intakes](loadtest/results/saturation-ae69a28/diagnostic-intake2/m11-11210202-ea57d-r5600-k1-w4/summary.json) |
| Apple M4 Pro, Docker Desktop | 1,400 normal events/s, 1,000 keys, 60 s offer, one normal rule worker | **1,007.83 processed events/s** across the offer window. Receiver **p50 15.165 s, p99 25.704 s**. The rule inbox grew during the offer and the run reconciled after drain. | [Machine](loadtest/results/saturation-10c398e-m4-pro/machine.json), [trial](loadtest/results/saturation-10c398e-m4-pro/k1000-w1-r1400/metrics.json) |
| Apple M4 Pro, Docker Desktop | Delivery batching, 700 normal + 2 critical events/s, 1,000 normal keys, 60 s offer plus 20 s drain, two intakes, four normal and one critical rule worker, one delivery worker | Normal receiver **p99 27.424 → 5.738 s** (2,000/2,000 receipts in each run), missing the <5 s target. Critical **p99 0.547 → 0.356 s** (240/240 each), meeting the <1 s target. | [Machine](https://github.com/yy31104/warsaw-hackathon-2026/blob/d873ee9a16c73f1fa17ba519379f44eb9b86ebd3/loadtest/results/w2-delivery-e2e-cc5bd7c/host.json), [paired trials](https://github.com/yy31104/warsaw-hackathon-2026/blob/d873ee9a16c73f1fa17ba519379f44eb9b86ebd3/docs/SCALING.md#delivery-batching--end-to-end), [scorecard](https://github.com/yy31104/warsaw-hackathon-2026/blob/d873ee9a16c73f1fa17ba519379f44eb9b86ebd3/docs/SCORECARD.md#w2-delivery-batching-result) |

The host series used different code revisions and traffic mixes. Each result applies to its stated workload. [Run conditions](docs/SCALING.md#apple-m4-pro-cross-machine-check)

## Supported event inputs

| Source | Supported payload | Test |
| --- | --- | --- |
| HTTP webhook | A JSON object or array of objects. A configured split path can extract an array, such as Alertmanager’s `alerts` envelope. | [HTTP ingestion](backend/tests/ingest/test_http.py) |
| RabbitMQ | A JSON object per message, including flat workflow events and nested `meta/payload` business events. | [Broker ingestion](backend/tests/ingest/test_amqp.py), [real RabbitMQ E2E](backend/tests/processing/test_broker_e2e.py) |
| Tailed log file | UTF-8 lines matched by a configured regex with named groups and optional logfmt key-value expansion. | [Log parsing](backend/tests/ingest/test_logfile.py), [tailing](backend/tests/ingest/test_tail.py) |

Unsupported event inputs on `main` include HTTP NDJSON, CSV, YAML, XML and plain-text log bodies, along with Kafka and polled API connectors. Use the registry to map a new schema within the supported payloads. [Registered sources](config/sources.yaml) · [Input contract](docs/CONTRACT.md#9-http-api-prefix-v1-owner-in-brackets)

## What is mocked

| Surface | Local behavior | Real integration |
| --- | --- | --- |
| SMS and phone | fake-telecom records messages and calls, and simulates callbacks and provider failures. | Replace the provider adapter in [telecom.py](backend/alerting/delivery/providers/telecom.py). |
| Email | The SMTP adapter sends to Mailpit, a local inbox. | Set the SMTP host and port. Add any required authentication and TLS in the [email adapter](backend/alerting/delivery/providers/email.py). |
| Workflow actions | mock-ops serves local operational targets such as workflow retries. | Wire each applicable [action adapter](backend/alerting/runbooks/actions/) to its real endpoint and credentials. |
| Sign-in | The demo supplies identity via the `X-User` header. The app checks team and approver roles on relevant mutations. | Use authenticated identity and enforce authorization at the API boundary. [Demo identity contract](docs/CONTRACT.md#9-http-api-prefix-v1-owner-in-brackets) |

## AI use

**In the product.** Optional assist suggests schema mappings, drafts rules from descriptions and structures runbook text for human review. The log file source uses a configured parser, and parser suggestion is under review. The sample configuration selects `gpt-6.2-sol` through the OpenAI provider adapter. Model calls are confined to configuration time. Ingest, matching, routing and delivery use the deterministic pipeline. With assist disabled, the provider unavailable or a response invalid, the tools fall back to local heuristics or manual review. [Provider adapter](backend/alerting/llm.py) · [Sample configuration](.env.example) · [Parser suggestion PR](https://github.com/yy31104/warsaw-hackathon-2026/pull/30)

On the frozen M3 synthetic final split, `gpt-6.2-sol` produced **17/17 expected field mappings** and **9/9 expected step outputs**, and passed **5/7 complete cases**. The local heuristic scored **11/17 mappings** and **7/9 steps**. The complete-case checks caught downstream severity and unit conversion misses. Replaying Sol’s outputs under the stricter approval policy yielded **7/9 exact steps** and caught **all 5 required approvals**, while requesting review for two extra steps. [M3 final score](loadtest/results/llm-eval-92f0e5c06bd9/final/summary.md) · [Approval follow-up and method](loadtest/results/llm-eval-92f0e5c06bd9/README.md)

**In development.** We used coding agents to help implement, review and test the app. We used synthetic fixtures for demos, tests and model evaluation.

## Privacy and security

- **Model requests.** For schema mapping, `describe_sample` creates a bounded list of field paths, types and example values. The mapping helper drops sensitive entries, omits object bodies and redacts or shortens text. It can include numeric and boolean examples for non-sensitive fields. Rule descriptions and runbook text are passed through the shared redactor before an optional provider call. [Sample and prompt redaction](backend/alerting/llm.py) · [Mapping prompt construction](backend/alerting/assist/schema_suggest.py) · [Rule and runbook text preparation](backend/alerting/assist/text_input.py)
- **Stored events.** `events.raw` is the parsed object after storage redaction. Values under matching key names such as `email`, `password`, `token`, `iban` and `account_number` are replaced as a whole. In other string values, pattern matches for email addresses, bearer or labelled credentials and international phone numbers are replaced. Values outside those patterns retain their types and full length. The queryable `fields` map is flattened from the redacted object, with schema aliases and numeric transforms applied. The SHA-256 payload hash uses the original canonical JSON for identity checks. [Normalization and redaction](backend/alerting/ingest/normalize.py) · [Inbox storage](backend/alerting/ingest/store.py)
- **Message masking and keys.** Schema-declared sensitive paths also mask notification content on external channels. The UI can display values that survived storage redaction. Keep real API keys in the ignored local `.env` and use synthetic data for this demo. [Channel masking](backend/alerting/routing/plan.py) · [Env template](.env.example) · [Git ignore rules](.gitignore)

## Limitations and next steps

- The demo uses a single PostgreSQL primary. Its CPU and I/O are shared by all workers, and updates to a single alert key serialize. Scale the stage whose queue grows and measure connection and lock pressure. When primary writes become the limit, evaluate partitioning by team or source and verify replay after failover. [Scaling limits and capacity planning](docs/SCALING.md)
- A saturated trace can end with broker-confirmed events or intended notifications pending. Generator non-submissions, durable intake and unique receiver receipts are tracked separately. Reconcile IDs through terminal state and verify how real providers handle an ambiguous timeout. [Delivery and loss accounting](docs/SCALING.md#delivery-and-loss-accounting)
- The batching trials still missed the normal receiver latency target. The remaining delay was concentrated before the first delivery claim in the slowest observed normal receipts. Profile intake, rule processing and the wait for delivery separately. The scorecard also calls for paired assist-on/off runs with provider-call telemetry and complete receipt coverage. [W0 scaling analysis](https://github.com/yy31104/warsaw-hackathon-2026/blob/d873ee9a16c73f1fa17ba519379f44eb9b86ebd3/docs/SCALING.md#delivery-batching--end-to-end) · [W0 acceptance scorecard](https://github.com/yy31104/warsaw-hackathon-2026/blob/d873ee9a16c73f1fa17ba519379f44eb9b86ebd3/docs/SCORECARD.md)

For development setup, configuration and gate records, see the [team brief](docs/BRIEF.md), [API contract](docs/CONTRACT.md) and [acceptance gates](docs/GATES.md).
