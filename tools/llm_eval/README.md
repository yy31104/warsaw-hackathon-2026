# M3 synthetic assist evaluation

This command exercises the same schema suggestion and runbook structuring functions as the public onboarding routes. Schema inputs pass through the production normalizer. A suggested schema is then applied to the same synthetic event to check its projected service, severity and time. Runbooks stay drafts throughout evaluation.

## Reproduce the campaign

Run from the repository root with the backend dependencies installed. Set `OPENAI_API_KEY` in the process environment for live measurements. Discovery and generation use the global OpenAI endpoint. The runner selects the latest matching dated snapshot listed for each reviewed model family, or the listed family ID when a dated snapshot is unavailable.

```sh
uv sync --project backend --frozen
export PYTHONPATH=.:backend
uv run --project backend python -m tools.llm_eval discover
uv run --project backend python -m tools.llm_eval baseline
# Apply development-driven improvements to the production assist.
uv run --project backend python -m tools.llm_eval tuned
uv run --project backend python -m tools.llm_eval final
uv run --project backend python -m tools.llm_eval budget
```

The independent synthetic corpus contains 12 schemas and 10 runbooks. Baseline and tuned use the same 8 schemas and 7 runbooks. Final evaluates the remaining 4 schemas and 3 runbooks with the tuned production code and model configuration. Development reads only the development fixtures and the SHA-256 manifest. The final command checks the completed tuned phase and frozen configuration before opening the two final fixtures. An interrupted run resumes unclaimed cases and replays completed results. A case interrupted after being claimed is recorded as `NOT_RUN` with its available receipt.

For a development-only, credential-free smoke test, use a new preview directory. Preview uses a temporary journal and runs all 15 development examples through the local heuristic. Both model modes are recorded as `NOT_RUN`. Passing `--offline` to an official phase records those skipped model results in the campaign journal.

```sh
uv run --project backend python -m tools.llm_eval preview --output "$(mktemp -d)"
uv run --project backend python -m pytest -q -c backend/pyproject.toml tools/llm_eval/test_eval.py
```

## Evidence and metrics

Each phase writes four files under `loadtest/results/llm-eval/<phase>/`. Use `--output DIR` to select their parent directory. Existing evidence is immutable. The SQLite campaign journal is separate from exported reports.

| File | Contents |
|---|---|
| `manifest.json` | Production source digest and Git revision, fixture and scorer digests, model inventory, model IDs, generation cap and rate card |
| `cases.jsonl` | One row per case and mode, expected annotations, actual product outputs, validated model hints, attribution, individual usage and latency |
| `summary.json` | Metric numerators and denominators, case statuses, normalization scores, provider coverage and outcomes, served identities, cost coverage and timing |
| `summary.md` | Product and provider-only results, development choice and cumulative budget exposure |

- **Field mappings** use exact canonical alias and path equality. The denominator includes every expected alias and every additional proposed alias, counting an incorrect path once. Exact-alias-set counts assess whole cases. Projection checks use the full draft and independently annotated service, severity, time format, UTC occurrence time and currency transforms.
- **Runbook steps** must match the instruction text, position, action, parameter keys and values, and approval. Extra and missing steps count against the denominator. A proposed parameter must be registered for the action and occur as a complete literal in its source instruction. The separate executable-step check also detects incorrect targets that happen to appear elsewhere in the same instruction. The approval checks include risky manual instructions, registered risky actions and dropped risky instructions.
- **Provider-only results** use actual validated responses with a verified model identity. For schemas, the expected mappings are the gaps left by the deterministic path. For runbooks, the scorer compares indexed candidate actions and parameters and checks abstention on manual steps. A valid response may contribute nothing to the product. Provider failures can still yield a usable local draft, with no provider-only score. The case file retains both outcomes.
- **Latency** uses nearest-rank p50 and p95 in milliseconds and reports sample counts. Endpoint time covers the cold production assist. The free input count runs outside this measurement. The M2 observer measures provider generation separately. Provider attempts with unavailable timing remain outside the latency denominator.
- **Cost** uses provider-reported input, cache-read, cache-write and output tokens, including usage returned with invalid or refused content. The report estimates USD from the published standard rates. Missing cache detail is priced conservatively and labeled `upper_bound`. Missing token totals leave the estimated cost unknown. Per-call receipts show both the priced amount and the amount held against the budget. Heuristic cases make zero billed calls. [API pricing](https://developers.openai.com/api/docs/pricing) · [Cache accounting](https://developers.openai.com/api/docs/guides/prompt-caching)
- **Selection** uses development results. The automatic comparison requires observed provider outputs on both tasks. An eligible model has zero product safety failures or invented parameters, a stable served identity, priced attempts, and successful provider outcomes. Ranking equally weights field and step accuracy, then breaks ties by mean priced call and endpoint p95. Final carries forward the frozen development decision. Missing comparative evidence yields `NOT_RUN`. If neither candidate qualifies, selection yields `FAIL`.

## Spending and reruns

The journal accounts for the **entire $5 campaign**, including interrupted requests. A generation has zero SDK retries, uses the default service tier and requests at most 1,536 output tokens. Before reserving funds, M2 asks the free Responses input-token endpoint to count the same model, instructions, redacted input and strict output schema that it will send for generation. The reservation adds the larger of 1,024 input tokens or 25% to that count, prices every input token as a 30-minute cache write, and applies long-context rates when applicable. An unavailable count uses the published maximum input or context limit. A reported bound breach stops subsequent attempts. [OpenAI token counting](https://help.openai.com/en/articles/4936856-understanding-and-counting-tokens) · [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) · [GPT-6.2 Sol](https://developers.openai.com/api/docs/models/gpt-6.2-sol)

SQLite commits each reservation before the paid request. A receipt releases only the difference supported by reported usage. Unknown billing retains the whole reservation. A process lock serializes phases and a unique case key prevents reissuing an attempt. Discovery, generation and receipts retain requested and reported model IDs. A base alias can resolve to an exact calendar-dated snapshot of the same model family.

Linked worktrees share `loadtest/results/tmp/m3-budget.sqlite3` in their primary checkout. For runs spanning independent clones on the same machine, set `W3_EVAL_JOURNAL` to the same absolute path before the first paid run and keep that journal for all phases. The cap covers calls issued by this runner.
