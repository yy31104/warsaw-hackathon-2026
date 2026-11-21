# Gate record

Status: **PASS / FAIL / NOT_RUN** only. Record who ran it, when, and the evidence (command, screenshot, result file). Yi signs off each gate. A FAIL is recorded as FAIL, never changed to NOT_RUN.

<!-- w0-round3-start -->
## Round 3 post-merge checks

Each row uses the actual GitHub merge commit on main, local PostgreSQL, RabbitMQ and the live simulated gateway.

For #19, the first run used the shared development database. Its team-scoped SSE assertion also received global `config.changed` frames from concurrent activity. The full rerun used a dedicated PostgreSQL database on the same host with the same Git tree. Subsequent W0 runs use that database.

| PR | Main | Status | Output | Evidence |
| --- | --- | --- | --- | --- |
| [#18](https://github.com/yy31104/warsaw-hackathon-2026/pull/18) | `13a9bd4` | PASS | 478 passed in 105.68s (0:01:45) | [make test](../loadtest/results/w0-round3/merges/pr-18-13a9bd4.log) |
| [#19](https://github.com/yy31104/warsaw-hackathon-2026/pull/19) | `adcd73b` | FAIL | 1 failed, 483 passed in 101.16s (0:01:41) | [make test](../loadtest/results/w0-round3/merges/pr-19-adcd73b.log) |
| [#19](https://github.com/yy31104/warsaw-hackathon-2026/pull/19) | `adcd73b` | PASS | 484 passed in 113.49s (0:01:53) | [make test](../loadtest/results/w0-round3/merges/pr-19-adcd73b-020116.log) |
| [#20](https://github.com/yy31104/warsaw-hackathon-2026/pull/20) | `de310f6` | PASS | 484 passed in 130.04s (0:02:10) | [make test](../loadtest/results/w0-round3/merges/pr-20-de310f6.log) |
<!-- w0-round3-end -->

## G0 — contract (Fri 21:00)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| All four read BRIEF + CONTRACT + own handoff | NOT_RUN | | |
| `make infra` + `make test` green on all four laptops | NOT_RUN | | |
| Golden expectations checked line by line against the printed task (Yi + daizhongtian) | NOT_RUN | | |
| Mentor answers: deadline, Lockbox path, 2.4 meaning | NOT_RUN | | |

## G1 — vertical slice (Sat 00:00)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| RabbitMQ → PostgreSQL inbox → rule worker → outbox → loopback HTTP receiver | PASS | Yi · 2026-11-20 | Production code from integrated main `accfd72`. `cd backend && INGEST_TEST_RABBITMQ_URL=amqp://localhost:5672/ uv run pytest -q tests/processing/test_broker_e2e.py`. One RabbitMQ publish produced one alert, two HTTP receipts (phone and SMS), one UI delivery and two successful provider attempts. With the broker enabled, the full suite passed 131 tests. |
| RabbitMQ `workflow_failed.json` → fake-telecom shows phone + SMS to sam | PASS | Yi · 2026-11-20 | Production services from main `b45e604`, test `49b1d68`. `cd backend && INGEST_TEST_RABBITMQ_URL=amqp://localhost:5672/ uv run pytest -q tests/processing/test_broker_e2e.py`. The production consumer wrote the inbox row. The rule worker created one alert, two pending phone/SMS notifications and a delivered UI notification. `/messages` returned one receipt for each expected notification ID, channel and on-call number. Both provider attempts were recorded as delivered. |
| Same event → UI card appears without refresh (SSE) | NOT_RUN | | |
| Branches merged into `main`, `make test` green | NOT_RUN | | |

## G2 — all example rules (Sat 04:00)
| Golden case group | Status | Who / when | Evidence |
|---|---|---|---|
| settlements-* (4) | NOT_RUN | | |
| rates-* (6) | NOT_RUN | | |
| anna-* + alertmanager-auto-resolve (7) | NOT_RUN | | |
| pricing-digest | NOT_RUN | | |
| retransmission-dedup, storm-500-one-alert | NOT_RUN | | |
| unknown-schema-visible | NOT_RUN | | |
| approval-required / wrong-person / ok | NOT_RUN | | |
| Three runbook formats render usable steps | NOT_RUN | | |

## G3 — feature freeze (Sat 07:30)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| channel-down-no-loss (0 lost) | NOT_RUN | | |
| Worker kill -9 recovery (ingest + delivery) | NOT_RUN | | |
| Private holdout (Marlon): unseen schemas onboarded without code (time each) | NOT_RUN | | |
| Private holdout (Marlon): unseen runbook → usable steps | NOT_RUN | | |
| Load test raw results committed (1 vs 2 workers, p50/p99 with N) | NOT_RUN | | |
| Fresh clone → first alert: commands + minutes (other laptop) | NOT_RUN | | |
| Rule preview leaves DB unchanged | NOT_RUN | | |
| Unaided UI tasks by a teammate (time) | NOT_RUN | | |

## G4 — safe submission (Sat 10:30)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| README: run steps, architecture, what is mocked, measured results, AI use + privacy | NOT_RUN | | |
| Video ≤ 5:00 uploaded to Lockbox `All Teams/<team>` | NOT_RUN | | |
| Text file with repo link uploaded | NOT_RUN | | |

## Golden run

On 2026-11-20 23:33 UTC, the pre-merge run at `e28da39` (including `main` at `47f7a31`) recorded **32 PASS, 0 FAIL, 0 NOT_RUN**. `tools/test_golden_isolated.ps1` started a fresh Compose project with RabbitMQ, the pricing file input, Mailpit and fake-telecom. The digest case waited for its five-minute delivery interval.

The same code passed 470 backend tests with PostgreSQL and RabbitMQ. The database upgrade was also run twice over persisted data. A volume initialized from the `47f7a31` migration files started the upgraded API, returned five rules and retained its audit record. The frontend passed 10 tests, ESLint and the real-API production build.

| Case | Status | Evidence |
|---|---|---|
| settlements-positive | PASS | All declared checks passed |
| settlements-negative-env | PASS | All declared checks passed |
| settlements-negative-workflow | PASS | All declared checks passed |
| settlements-runbook-rendered | PASS | All declared checks passed |
| rates-positive | PASS | All declared checks passed |
| rates-boundary-equal | PASS | All declared checks passed |
| rates-other-desk | PASS | All declared checks passed |
| rates-schema-glob-v3 | PASS | All declared checks passed |
| rates-not-rejected | PASS | All declared checks passed |
| rates-missing-template-field | PASS | All declared checks passed |
| anna-daytime-sms | PASS | All declared checks passed |
| anna-evening-email | PASS | All declared checks passed |
| anna-boundary-0800 | PASS | All declared checks passed |
| anna-boundary-2000 | PASS | All declared checks passed |
| anna-not-critical | PASS | All declared checks passed |
| anna-other-service | PASS | All declared checks passed |
| alertmanager-auto-resolve | PASS | All declared checks passed |
| pricing-digest | PASS | All declared checks passed |
| retransmission-dedup | PASS | All declared checks passed |
| storm-500-one-alert | PASS | All declared checks passed |
| unknown-schema-visible | PASS | All declared checks passed |
| approval-required | PASS | All declared checks passed |
| approval-wrong-person | PASS | All declared checks passed |
| approval-ok | PASS | All declared checks passed |
| channel-down-no-loss | PASS | All declared checks passed |
| stale-recovery-keeps-newer-episode | PASS | All declared checks passed |
| poison-event-isolated | PASS | All declared checks passed |
| inbound-approval-needs-provider-secret | PASS | All declared checks passed |
| inbound-approval-brute-force-locked | PASS | All declared checks passed |
| verify-unit-change | PASS | All declared checks passed |
| rule-safety-counterexample | PASS | All declared checks passed |
| rule-safety-ui-only-is-valid | PASS | All declared checks passed |
