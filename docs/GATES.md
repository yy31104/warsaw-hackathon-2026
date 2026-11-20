# Gate record

Status: **PASS / FAIL / NOT_RUN** only. Record who ran it, when, and the evidence (command, screenshot, result file). Yi signs off each gate. A FAIL is recorded as FAIL, never changed to NOT_RUN.

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
