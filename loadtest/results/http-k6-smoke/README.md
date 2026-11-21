# HTTP k6 smoke · 20 November 2026

The official Grafana k6 2.4.0 Docker image ran against the isolated `dev2perfk6` API and normal worker. The script offered 20 synthetic `generic-webhook` requests per second for five seconds with five preallocated VUs and a maximum of 20. Each iteration sent one event with a distinct ID.

| Metric | Observed |
|---|---:|
| Completed iterations | 100 |
| HTTP requests | 100 |
| Successful durable-acceptance checks | 100 |
| `dropped_iterations` | 0 |
| Distinct processed event IDs read back from API | 100 |
| HTTP request duration p50 | 11.48 ms |
| HTTP request duration p99 | 20.61 ms |

Both k6 thresholds passed. The event query found the exact IDs `dev2-http-smoke-final-0` through `dev2-http-smoke-final-99` and a generation timestamp on each processed event. The request-duration percentiles measure only this HTTP call. [Raw k6 output](k6-summary.json) · [Event ID ledger](event-ledger.json)

[Replay instructions](../../README.md#http-ingress-smoke-test) · [Host and Docker environment](../platform-bench/machine.json)
