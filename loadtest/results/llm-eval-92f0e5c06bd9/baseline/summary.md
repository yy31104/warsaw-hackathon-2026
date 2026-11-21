# M3 baseline

| Mode | PASS / FAIL / NOT_RUN cases | Correct fields | Correct steps | Invented params | Risky without approval | Assist p50 / p95 ms | API calls | Mean USD / costed call |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| heuristic | 8 / 7 / 0 | 23/38 | 14/16 | 0 | 0 | 0.273 / 1.094 | 0 | — |
| gpt-6-luna | 11 / 4 / 0 | 35/38 | 16/16 | 0 | 0 | 1553.884 / 2633.594 | 14 | 0.00007481428571428571428571428571 |
| gpt-6.2-sol | 11 / 4 / 0 | 35/38 | 16/16 | 0 | 0 | 1487.205 / 2958.625 | 14 | 0.001160142857142857142857142857 |

| Mode | Provider-only gap mappings | Provider-only step classifications | Model contributed | Valid response, no contribution | Failed attempt, local fallback | Local only |
|---|---:|---:|---:|---:|---:|---:|
| heuristic | — | — | 0 | 0 | 0 | 0 |
| gpt-6-luna | 12/13 | 16/16 | 5 | 9 | 0 | 1 |
| gpt-6.2-sol | 12/13 | 16/16 | 5 | 9 | 0 | 1 |

Development selection **PASS**. gpt-6-luna. safe development result ranked by mean field/step accuracy, then cost and endpoint p95.

Cumulative known or conservatively reserved charges across phases: $0.0172894 of $5.

Per-case outputs, expected labels, provider receipts and timing are in `cases.jsonl`. Counts, projection metrics and pricing coverage are in `summary.json`.
