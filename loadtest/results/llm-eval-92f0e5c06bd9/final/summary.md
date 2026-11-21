# M3 final

| Mode | PASS / FAIL / NOT_RUN cases | Correct fields | Correct steps | Invented params | Risky without approval | Assist p50 / p95 ms | API calls | Mean USD / costed call |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| heuristic | 3 / 4 / 0 | 11/17 | 7/9 | 0 | 1 | 0.372 / 1.049 | 0 | — |
| gpt-6-luna | 5 / 2 / 0 | 17/17 | 9/9 | 0 | 0 | 3132.23 / 6227.01 | 7 | 0.0001575714285714285714285714286 |
| gpt-6.2-sol | 5 / 2 / 0 | 17/17 | 9/9 | 0 | 0 | 1591.313 / 2112.882 | 7 | 0.001398285714285714285714285714 |

| Mode | Provider-only gap mappings | Provider-only step classifications | Model contributed | Valid response, no contribution | Failed attempt, local fallback | Local only |
|---|---:|---:|---:|---:|---:|---:|
| heuristic | — | — | 0 | 0 | 0 | 0 |
| gpt-6-luna | 6/14 | 9/9 | 3 | 4 | 0 | 0 |
| gpt-6.2-sol | 6/6 | 9/9 | 3 | 4 | 0 | 0 |

Development selection **PASS**. gpt-6-luna. safe development result ranked by mean field/step accuracy, then cost and endpoint p95.

Cumulative known or conservatively reserved charges across phases: $0.0475149 of $5.

Per-case outputs, expected labels, provider receipts and timing are in `cases.jsonl`. Counts, projection metrics and pricing coverage are in `summary.json`.
