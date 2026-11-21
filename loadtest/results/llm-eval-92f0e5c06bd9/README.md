# Alert Funnel assist evaluation

Use `ASSIST_PROVIDER=openai` and `ASSIST_MODEL=gpt-6-luna` for the demo. The tuned development run gave both models 37/37 field mappings and 16/16 steps. Luna cost less per call. Sol had the lower p95 latency on both the tuned development and final splits.

| Split | Mode | Fields | Steps | Invented params | Risky without approval | Assist p50 / p95 (ms) | Calls | Input / output tokens | USD/call |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | heuristic | 23/38 | 14/16 | 0 | 0 | 0.3 / 1.1 | 0 | 0 / 0 | 0 |
| baseline | gpt-6-luna | 35/38 | 16/16 | 0 | 0 | 1553.9 / 2633.6 | 14 | 4059 / 1283 | 0.0000748 |
| baseline | gpt-6.2-sol | 35/38 | 16/16 | 0 | 0 | 1487.2 / 2958.6 | 14 | 4661 / 692 | 0.0011601 |
| tuned | heuristic | 27/37 | 16/16 | 0 | 0 | 0.2 / 0.9 | 0 | 0 / 0 | 0 |
| tuned | gpt-6-luna | 37/37 | 16/16 | 0 | 0 | 1874.0 / 5533.8 | 14 | 4560 / 2242 | 0.0001126 |
| tuned | gpt-6.2-sol | 37/37 | 16/16 | 0 | 0 | 1500.8 / 2589.2 | 14 | 5162 / 734 | 0.0012617 |
| final | heuristic | 11/17 | 7/9 | 0 | 1 | 0.4 / 1.0 | 0 | 0 / 0 | 0 |
| final | gpt-6-luna | 17/17 | 9/9 | 0 | 0 | 3132.2 / 6227.0 | 7 | 2430 / 1720 | 0.0001576 |
| final | gpt-6.2-sol | 17/17 | 9/9 | 0 | 0 | 1591.3 / 2112.9 | 7 | 2709 / 437 | 0.0013983 |

Baseline and tuned used the same eight synthetic events and seven runbooks. One erroneous extra baseline alias increased the field denominator from 37 to 38. The final holdback contained four events and three runbooks. The team froze the selection and production source before opening that split. End-to-end timings here cover local normalization and assist logic, including a required provider call. They exclude the external HTTP hop into the app and the free evaluation-only input counter.

The 70 benchmark generations cost an estimated **$0.0474214** at the recorded standard token rates. A separate live M2 HTTP smoke used one generation costing an estimated **$0.0000935**, bringing total measured usage to **$0.0475149** against the **$5** limit. All paid calls returned token receipts. These are token-based calculations, not a billing invoice.

Both model-assisted modes matched all held-back aliases and steps. Each passed 5/7 complete cases. The downstream severity projection scored 3/4 and the declared minor-unit transform scored 0/1. The original heuristic missed one required approval, which fails the zero-tolerance gate on the frozen build.

The frozen benchmark uses source and fixture hashes in each manifest. Production baseline is reproduced by commit `e082140`; tuned and final production by `92f0e5c06bd9`. Baseline and tuned ran while working-tree changes were awaiting commit. See the source SHA-256 alongside the Git revision in each manifest.

The three phase directories contain per-case outputs, token receipts, normalized scoring and latency distributions. The pricing sources and commands are in [the evaluator guide](../../../tools/llm_eval/README.md).

## Approval follow-up

The policy author used the public action registry and existing backend tests. A separate author froze 12 risky instructions and two read-only controls before the follow-up policy was frozen. In each of the no-key, simulated HTTP 503, and disconnected modes, the earlier build left 6/12 risky instructions unguarded and required review for 1/2 controls. The follow-up guarded all 12/12 in each mode. It required review for 2/2 controls. All model traffic in this independent check was mocked.

After freezing the new policy, we replayed only its approval decision against the nine original emitted steps per mode. This used no model, did not reopen the synthetic holdback inputs, and left the original blind report intact. The new policy missed 0/5 required approvals in each mode. It added three unnecessary reviews to the heuristic output and two to each model's output. With the changed approval labels, exact step scores became 5/9 for the heuristic and 7/9 for each model. These are post-fix regression results. The full latency and accuracy benchmark above describes the frozen earlier build.

```sh
uv run --project backend python -m pytest -q -s -c backend/pyproject.toml tools/llm_eval/test_approval_acceptance.py
PYTHONPATH=.:backend uv run --project backend python -m tools.llm_eval.replay_approvals loadtest/results/llm-eval-92f0e5c06bd9/final/cases.jsonl
```

The original candidates, including the two incomplete downstream projections, remain visible in the [blind report](final/summary.md). The [replay output](approval-replay.json) records adjustments separately.
