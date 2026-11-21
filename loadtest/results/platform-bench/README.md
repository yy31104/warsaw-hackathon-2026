# Platform benchmark · 20 November 2026

The final trials used integrated source `29b0dcef6c6ff9b5f780e11a60513d812d564120` and fresh Compose projects, networks and volumes on Docker Desktop. The host was an Intel Core Ultra 9 275HX with a 24-CPU WSL2 VM and 33.35 GB of VM memory. Full Compose startup with cached image layers took 20.79 s for the 13-service stack. The built frontend ran with `VITE_MOCK=0` and served a processed pricing log event through the same-origin API proxy. [Machine](machine.json) · [Compose evidence](one-normal/stack.json)

## Worker comparison

Both runs offered the same 14,040 RabbitMQ events over 20 s at 700 normal and 2 critical events/s. Normal events shared one alert key. Each critical event had its own key. One critical worker and one delivery worker ran in both trials. The workload hash was `3ff3cc90ef376f2489aedf1df1cef74270015a9cf684130aee42b79d7c240e7f`. Each run durably stored and processed all 14,040 events, with zero generator drops or publish errors. [One normal worker](one-normal/summary.json) · [Two normal workers](two-normal/summary.json)

| Normal workers | Ingest events/s | Processed events/s | Processing window | Receiver N | p50 | p95 | p99 | Peak sampled inbox backlog |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 702.00 | 699.33 | 20.076 s | 82 | 236 ms | 366 ms | 435 ms | 174 |
| 2 | 702.00 | 700.12 | 20.054 s | 82 | 234 ms | 346 ms | 376 ms | 175 |

For the critical subset, the 1-worker run had p50 233 ms and p99 380 ms, and the 2-worker run had p50 234 ms and p99 364 ms. Both critical samples contained 80 independently recorded SMS/phone receipts. The two setups kept up with the offered input in this single pair of 20-second trials.

The funnel and Prometheus counters both recorded 14,047 stored events, including seven startup log lines outside the benchmark. The latency histogram on `/metrics` counted 82 provider confirmations. The latency percentiles above use the simulated phone's own receipt times. [Funnel](one-normal/funnel.json) · [Prometheus](one-normal/metrics.prom)

## Telecom stop and recovery

The fake-telecom container stayed stopped for 30.020 s before Docker received the start request. Docker brought back the same container and receipt volume. The host measured the stop interval with a monotonic clock. The observer inside the Docker VM probed gateway availability and observed all 12 normal settlement events processed with 24 telecom notifications still queued. There were zero receipts during the interruption. [Container evidence](telecom-outage/outage-container.json) · [Outbox and receiver ledger](telecom-outage/summary.json)

After recovery, all 24 original IDs appeared exactly once in the independently recorded phone receipts. Seven IDs succeeded after an earlier attempt. The expected and observed ID sets matched, with **0 lost**. The first generation to last receipt spanned 38.889 s. The receiver reported p50 28.393 s and p99 38.399 s across 24 receipts.

The separate critical-event trial held the container stopped for 30.016 s before issuing the start request. It observed 24 queued original IDs and zero receipts during the interruption, then recovered all 24 original IDs, including seven that retried. The trial had **0 lost** and measured p50 28.404 s and p99 38.450 s for 24 receipts. The open circuit recovered through the original channels in this trial. [Critical container evidence](critical-outage/outage-container.json) · [Critical receiver ledger](critical-outage/summary.json)

## Earlier critical HTTP 503 rehearsal

The gateway returned HTTP 503 in demo mode for 30.021 s during this rehearsal. The outbox recorded 24 expected critical SMS/phone IDs and the receiver recorded zero primary receipts. The critical fallback policy moved all 24 to replacement records. PostgreSQL recorded 12 delivered UI replacements and successful SMTP acknowledgements for 12 email replacements. The earlier rehearsal did not independently read back those messages from the Mailpit API. The primary-channel result for this rehearsal was **FAIL, 24 original IDs missing**. [Primary ledger](critical-http-503-rehearsal/summary.json) · [Replacement records](critical-http-503-rehearsal/fallback-db.json)
