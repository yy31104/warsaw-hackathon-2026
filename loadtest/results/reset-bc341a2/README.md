# Demo reset and worker measurements

The measured source was `bc341a2` on `feat/data-daizhongtian`, after merging `main` `47f7a31` into DEV2 `6f0ee45`. The trials ran on 21 November 2026 (Europe/Warsaw). Each trial used a new Compose project, PostgreSQL database, RabbitMQ queue and simulated phone volume. [Machine and exact source revision](machine.json)

## Reset regression

| Check | Result |
| --- | --- |
| Original integration | The two existing reset tests failed on the PostgreSQL foreign key from `heartbeat_watches` to `alerts`. |
| Catalog after 28 migrations | PASS. All 23 application tables were classified. One explicit `TRUNCATE ... RESTART IDENTITY CASCADE` clears 19 runtime tables including watches, silences and delivery turns. Three config tables and one input cursor table are preserved. The catalog guard detected both an unclassified test table and a test foreign key that would cascade into preserved configuration. |
| Real API with `DEMO_MODE=0` | PASS. HTTP 404. The seeded event, alert and linked watch remained. |
| Real API with `DEMO_MODE=1` | PASS. Two consecutive HTTP requests returned 200. The first cleared the event, alert and watch. All three tables were still empty after the second. Every one of the 39 loaded config rows matched its snapshot. The first new event received ID 1. |
| Log input between cases | PASS. With `log_tail_checkpoints` cleared, an unchanged file recreated a consumed event after reset. Preserving its cursor kept the inbox empty until a new line was appended, and only that new line appeared. |
| Tests | PASS. Six targeted tests and all 466 tests under `make test` passed with PostgreSQL 16, RabbitMQ and the live canary gateway enabled. |

Schemas, rules, teams, user preferences and runbooks live in `config_objects`. The other configuration tables are `config_state` and `backend_file_rule_revisions`.

## Hot key and distributed keys

Every healthy trial offered 14,000 normal and 40 critical settlement events over 20 seconds. Critical events used distinct keys and arrived twice each second. Normal events either shared a single key or rotated over 256 keys, each of which received 54 or 55 events. The trials varied the number of normal rule workers and held one critical rule worker and one delivery worker fixed.

| Normal keys | Normal workers | Input events | Ingest/s | Processed/s | All receipt p50 / p99 (ms) | All receipts N | Critical p50 / p99 (ms) | Critical receipts N | Lost / duplicate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [Hot (1)](one-normal/summary.json) | 1 | 14,040 | 702.0 | 697.3 | 241 / 344 | 82 | 237 / 344 | 80 | 0 / 0 |
| [Hot (1)](two-normal/summary.json) | 2 | 14,040 | 702.0 | 699.4 | 241 / 381 | 82 | 240 / 381 | 80 | 0 / 0 |
| [Distributed (256)](one-spread/summary.json) | 1 | 14,040 | 702.0 | 603.2 | 5,497 / 12,278 | 592 | 210 / 281 | 80 | 0 / 0 |
| [Distributed (256)](two-spread/summary.json) | 2 | 14,040 | 702.0 | 698.9 | 3,501 / 6,817 | 592 | 208 / 361 | 80 | 0 / 0 |

The one- and two-worker runs for each key distribution used identical message IDs, JSON payloads and scheduled offsets. Across distributions only the normal grouping field changed. Throughput includes the time to drain accepted events. End-to-end latency runs from the generator timestamp to the independent SMS or call receipt. Percentiles use nearest rank. Each row summarizes one run at the stated offered rate.

All four trials produced 4,000 decision rows per 1,000 input events. The hot-key trials delivered 2.92 SMS per 1,000 events and the distributed trials delivered 21.08. The oldest pending notification sampled for 1 / 2 normal workers was 0.15 / 0.17 seconds on one key and 11.85 / 6.51 seconds on 256 keys. With 256 keys, the second worker brought overall p99 from 12.278 s to 6.817 s in these trials. Critical p99 was 281 ms and 361 ms respectively.

## Simulated telecom outage

The host held the fake-telecom container stopped for **30.018 seconds** before requesting a restart. During the outage the generator sent 12 separately grouped settlement events over six seconds, the database queued 24 original SMS and call notifications, and the receiver recorded zero deliveries. After restarting the same container with the same receipt volume, the receiver confirmed all 24 original IDs. Seven notifications retried. **Lost 0, duplicates 0.** Generator-to-receiver p50 was 28.465 s and p99 was 38.523 s across 24 receipts. [Stop and restart times](telecom-outage/outage-container.json) · [Delivery reconciliation](telecom-outage/summary.json)

## Reproduction

Run in PowerShell 7 from the repository root with Docker Desktop. The command uses new Compose projects and a separate output directory.

```powershell
$trials = 'one-normal', 'two-normal', 'one-spread', 'two-spread', 'telecom-outage'
./loadtest/run-platform-bench.ps1 -Trials $trials -Output 'loadtest/results/tmp/reset-repeat'
```

Run `make test` at the repository root with the CI PostgreSQL, RabbitMQ and live gateway settings. The Windows helper provisions the services and runs the same pytest suite.

```powershell
$targets = 'tests/ingest/test_ops.py', 'tests/ingest/test_tail.py::test_reset_keeps_consumed_log_lines_out_of_the_next_case'
./tools/test_isolated.ps1 -TestPath $targets
./tools/test_isolated.ps1
```

Each trial directory contains the generator, database, outbox and receiver records in `raw.json.gz`, its SHA-256 checksum, observed service counts, funnel and Prometheus snapshots. All five checksums, actual input schedules for each worker pair, receipt IDs and latency percentiles were independently checked against the saved records. W0 owns the separate 32-case golden run after the upstream merge.
