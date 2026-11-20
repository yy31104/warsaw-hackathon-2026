# Alert Funnel platform tests

## Local Compose demo

```powershell
docker compose up -d --build --wait
```

The UI listens on port 5173 and uses the real API through its same-origin proxy. The API listens on port 8000, the simulated phone on port 8100, Mailpit on port 8025 and mock ops on port 8200. PostgreSQL and RabbitMQ have their own named volumes. Each new database volume applies the migrations on first startup.

The pricing log tailer reads `pricing-engine.log` in the host directory set by `LOG_DIR`. The default demo directory is `fixtures/events`. To run in parallel with another stack, set a distinct Compose project name and override all the published ports. The benchmark port file is one example.

`GET /v1/stats/funnel?window=15m` reads inbox, rule and outbox data. The window limits arrivals and notification history. Open alerts and pending backlogs are current gauges. The displayed per-second rates use the latest two seconds. `GET /metrics` exposes the database counts and per-channel/lane delivery risk in Prometheus text. Its confirmation histogram ends when the delivery worker records provider success. The benchmark below measures arrival at the receiver.

`POST /v1/debug/reset` clears runtime data in demo mode. Config, log checkpoints and provider receipts stay available for the next run. Set `DEMO_MODE=0` to disable the reset.

## Worker comparison and outage

```powershell
pwsh -File loadtest/run-platform-bench.ps1
```

Run this from the repository root with Docker Desktop, Docker Compose and PowerShell 7. The script allocates a new Compose project and new broker, database and phone volumes for each trial, uses reserved loopback ports in `isolated-ports.env`, writes evidence to `loadtest/results/tmp/platform-bench/`, then removes the trial containers. The named volumes can be inspected afterward.

- Both worker trials offer 700 normal and 2 critical RabbitMQ events/s for 20 s. Normal events share one alert key. Each critical event gets its own alert key. The script records and checks worker counts, holding one critical worker and one delivery worker in both trials. The workload hash covers scheduled ticks, message IDs and JSON bodies. It excludes wall-clock generation timestamps.
- The outage trial uses 12 separately grouped normal settlement events over six seconds, each expected to produce a phone and SMS delivery. The host stops the fake-telecom container, verifies it is unreachable, waits 30 seconds from the completed stop and restarts the same container with its original receipt volume. A separate probe watches availability as the generator submits the events. After recovery, the collector joins the planned outbox UUIDs and the provider's idempotency keys. Loss for the requested channels is the count of expected group/channel slots with no verified receipt.
- The optional `-Trials critical-outage` uses critical events to exercise the existing channel fallback policy. The trial reports failure if an original telecom ID has no receipt. The evidence records verified fallback deliveries and remaining logical loss separately. Mailpit verifies email IDs and recipients. UI deliveries are durable rows in PostgreSQL.

Throughput divides stored, processed or receiver counts by the elapsed interval from generator start through the last observed stage timestamp, including at least the full offer window. Inbox arrival uses the recorded consumer callback time on committed event rows. Processing uses the worker's completion trace. End-to-end latency uses the receiver's recorded epoch milliseconds minus the generator timestamp, with nearest-rank p50, p95 and p99 and a sample count. Both clocks are in the same Docker VM.

Each result directory stores `stack.json`, `summary.json` and compressed `raw.json.gz` with its SHA-256. The raw data includes the actual send, broker, event, outbox, receiver and polling records. Successful trials also capture `funnel.json` and `metrics.prom`. The outage adds the host stop/start record in `outage-container.json`. The parent directory records the machine and Docker environment in `machine.json`.

- `golden_runner.py` — owner DEV1 Yi (moved Fri 22:00).
- `bench_generate.py`, `bench_collect.py` and `platform_bench.py` — DEV2 workload, telemetry and reconciliation.
