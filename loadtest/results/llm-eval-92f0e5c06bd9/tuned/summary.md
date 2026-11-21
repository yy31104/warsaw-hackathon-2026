# M3 tuned

| Mode | PASS / FAIL / NOT_RUN cases | Correct fields | Correct steps | Invented params | Risky without approval | Assist p50 / p95 ms | API calls | Mean USD / costed call |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| heuristic | 10 / 5 / 0 | 27/37 | 16/16 | 0 | 0 | 0.239 / 0.935 | 0 | — |
| gpt-6-luna | 13 / 2 / 0 | 37/37 | 16/16 | 0 | 0 | 1874.026 / 5533.798 | 14 | 0.0001126428571428571428571428571 |
| gpt-6.2-sol | 13 / 2 / 0 | 37/37 | 16/16 | 0 | 0 | 1500.844 / 2589.172 | 14 | 0.001261714285714285714285714286 |

| Mode | Provider-only gap mappings | Provider-only step classifications | Model contributed | Valid response, no contribution | Failed attempt, local fallback | Local only |
|---|---:|---:|---:|---:|---:|---:|
| heuristic | — | — | 0 | 0 | 0 | 0 |
| gpt-6-luna | 10/16 | 16/16 | 4 | 10 | 0 | 1 |
| gpt-6.2-sol | 10/10 | 16/16 | 4 | 10 | 0 | 1 |

Development selection **PASS**. gpt-6-luna. safe development result ranked by mean field/step accuracy, then cost and endpoint p95.

Cumulative known or conservatively reserved charges across phases: $0.0366239 of $5.

Per-case outputs, expected labels, provider receipts and timing are in `cases.jsonl`. Counts, projection metrics and pricing coverage are in `summary.json`.
