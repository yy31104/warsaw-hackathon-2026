# Acceptance targets

| Experiment | Registration | Target |
| --- | --- | --- |
| M3 · Assist quality | target set after measurement | On the frozen synthetic field-mapping and runbook cases, score each of heuristic and model modes separately. Target ≥90% correct expected mappings and ≥90% correct expected steps, zero invented action parameters and zero risky actions without approval. Include every eligible case in its denominator. |
| M11 · Critical receiver latency | target set after measurement | On the Intel Core Ultra 9 275HX, offer 700, 1,400 and 2,800 normal events/s in separate trials, plus 2 critical/s, across 1,000 normal keys. Use one intake, four normal rule workers, one critical worker and one delivery worker. For each 60 s offer plus 20 s observation after the offer, target critical receiver p99 <1 s and all scheduled critical SMS/phone slots verified by cutoff. |
| M11 · Event accounting at saturation | target set after measurement | On the Apple M4 Pro at 5,600 normal + 2 critical events/s, 1,000 normal keys, four normal rule workers, one critical and one delivery worker, compare one and two intakes using the same 60 s schedule and a 20 s drain. Target full ID reconciliation across submission, broker confirmation, durable intake and processing. Reconcile logical notification slots independently against receiver receipts. |
| Telecom outage · durable delivery | target set after measurement | On the Intel Core Ultra 9 275HX, offer 2 critical events/s for 6 s, no normal traffic, one worker in each rule lane and one delivery worker. Stop the simulated telecom receiver for 30 s. Target every accepted event processed and every intended original SMS/phone ID independently receipted within 90 s of restarting the receiver. Target zero duplicate original receipts. Account for fallback receipts separately. |
| W2 delivery batching · end-to-end notification latency | pre-registered | Target normal p99 <5 s and critical p99 <1 s on Apple M4 Pro, at 700 normal events/s plus 2 critical/s, 1,000 normal keys, 4 normal rule workers, 1 critical rule worker, and 1 delivery worker. Use two intake consumers, a 60 s offer and a 20 s post-offer observation window. Require receipt coverage to be reported separately for each lane. |
| Alert path · LLM calls | pre-registered | Trace the production paths from ingest through rule evaluation and delivery. Target zero reachable LLM calls on those paths and zero provider calls attributable to their event or notification traces in the paired test below. Verify the provider call logger with a separate synthetic assist request before the timed window. Missing provider telemetry makes the result inconclusive. |
| Alert path · assist on/off latency | pre-registered | On the Apple M4 Pro, offer exactly 700 events/s (698 normal + 2 critical), 1,000 normal keys, two intakes, four normal rule workers, one critical and one delivery worker. Use ten paired trials with alternating order, identical schedules within each pair, fresh volumes, 60 s offers and 20 s drains. Target no significant practical change in receiver p99 for either lane when assist is enabled. For each lane, the 90% confidence interval for the paired geometric mean of on/off p99 ratios must fit within 0.90–1.10. Use trial pairs as the units and paired log ratios for the interval. Require complete expected receipts in both arms to claim equivalence. |
| Decision stability | pre-registered | Held-out: proposed asks fewer questions than the ask-every-unknown baseline, routing is correct on 4/4 cases, and critical misses are zero. Report questions, routing correctness, critical misses, INCOMPLETE counts and runtime p50/p95 separately for development and held-out cases. |

**Accounting target for every traffic experiment.** Report offered, generator-not-submitted, publish errors, broker-confirmed (accepted by the broker), durably ingested, and accepted events pending at the end of the stated window. Split pending events between the broker and the rule inbox. For notifications, report intended logical slots, delivered unique receiver receipts, pending or unverified slots, and duplicates. State each denominator and cutoff, including when a count is zero.

## W2 delivery batching result

**Normal receiver p99 <5 s · FAIL.** On the pre-registered Apple M4 Pro workload and the same workload hash, normal receiver p99 was 27.424 s before batching (2,000 received / 2,000 expected) and 5.738 s after batching (2,000 / 2,000). Critical receiver p99 was 0.547 s before (240 / 240) and 0.356 s after (240 / 240), meeting the <1 s target. [Paired end-to-end trials](SCALING.md#delivery-batching--end-to-end)

## Decision stability result

| Split | Method | Mean questions per case | Routing correct | Critical misses | INCOMPLETE | Runtime p50 / p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Development (8) | Ask all | 2.25 | 8/8 | 0/2 | 0 | 0.453 / 1.918 ms |
| Development (8) | Adaptive minimax | 1.25 | 7/8 | 0/2 | 1 | 0.776 / 1.544 ms |
| Held-out (4) | Ask all | 2.75 | 4/4 | 0/2 | 0 | 0.486 / 0.760 ms |
| Held-out (4) | Adaptive minimax | 1.00 | 4/4 | 0/2 | 0 | 1.383 / 1.713 ms |

**Held-out targets · PASS.** The Apple M4 Pro run used 12 synthetic cases and 14 samples, with one timed run per method and case. The development case with 81 interpretations returned `INCOMPLETE` at the 64-combination cap. It counts against correctness and is charged the full number of questions. [Case results and method](../loadtest/results/stability-cb87120d1055/summary.md) · [Raw measurements](../loadtest/results/stability-cb87120d1055/report.json)

<!-- w0-release-scores-start -->
## Final release acceptance

Release SHA `1b82645640736f48f31da6f41945cfae8cd93b0b`. Frontend, browser, startup and innovation measurements use this SHA. Backend, golden and input-variation measurements use `dd9d4834216f63ded7dabd130ea6b58ea62c1d3f`, with identical backend, Compose configuration and golden inputs after the frontend-only edits. [Command receipts](../loadtest/results/w0-round3/final-1b82645640-release-final/suite.json)

| Measure | Status | Observed value | Evidence |
| --- | --- | --- | --- |
| Fresh clone to README alert | PASS | 25.079 s (one timed run, fresh volumes and warm Docker cache) | [Timing](../loadtest/results/w0-round3/final-1b82645640-release-final/first-alert.json) |
| Backend tests | PASS | 798/798 passed, 0 skipped | [pytest](../loadtest/results/w0-round3/final-1b82645640-release-final/backend-tests.log) |
| Frontend tests | PASS | 19/19 passed | [Vitest](../loadtest/results/w0-round3/final-1b82645640-release-final/frontend-tests.log) |
| Frontend lint | PASS | `npm run lint` | [ESLint](../loadtest/results/w0-round3/final-1b82645640-release-final/frontend-lint.log) |
| Frontend production build | PASS | One production build | [Build log](../loadtest/results/w0-round3/final-1b82645640-release-final/frontend-build.log) |
| Golden cases | PASS | 32/32 passed, 0 failed, 0 not run | [Golden results](../loadtest/results/w0-round3/final-1b82645640-release-final/golden.log) |
| Real API console scenarios | PASS | 13/13 completed checks passed | [Playwright](../loadtest/results/w0-round3/final-1b82645640-release-final/browser/result.json) |
| Initial browser run | FAIL | Reused alert IDs matched prior golden receipts. | [Initial output](../loadtest/results/w0-round3/final-1b82645640-release-final/browser-first-attempt.log) · [Retry command](../loadtest/results/w0-round3/final-1b82645640-release-final/browser-clean-receipt.json) |
| Clean browser retry, original harness | FAIL | The original test expected dark in default light sessions. Browser results above include the test-only fix. | [Original retry](../loadtest/results/w0-round3/final-1b82645640-release-final/browser-original-clean-retry.log) |
| Innovation browser walkthrough | PASS | 8/8 checks passed on product commit `1b82645` | [Playwright](../loadtest/results/w0-round3/final-1b82645640-release-final/innovation-browser/result.json) |

### Innovation on final main

| Measure | Status | Observed value | Evidence |
| --- | --- | --- | --- |
| Unknown outcome reconciliation | PASS | One run recovered the original receipt. Confirmed retry HTTP 409. Receiver executions 1/1 expected. | [Live check](../loadtest/results/w0-round3/final-1b82645640-release-final/unknown-verify.json) |
| Decision stability | PASS | Held-out routing 4/4, critical misses 0/2. Mean questions 2.75 ask-all and 1.00 adaptive across 4 held-out cases. | [stability-eval.log](../loadtest/results/w0-round3/final-1b82645640-release-final/stability-eval.log) · [stability-eval-report.json](../loadtest/results/w0-round3/final-1b82645640-release-final/stability-eval-report.json) · [stability-eval-summary.md](../loadtest/results/w0-round3/final-1b82645640-release-final/stability-eval-summary.md) · [stability-live.json](../loadtest/results/w0-round3/final-1b82645640-release-final/stability-live.json) |
| Policy optimizer | PASS | 18 baseline and 4 candidate planned nighttime interruptions on 18 synthetic seed events. 0/14 protected cases violated. Rejected option had a missing SMS counterexample. | [optimizer-demo.log](../loadtest/results/w0-round3/final-1b82645640-release-final/optimizer-demo.log) · [optimizer-live.json](../loadtest/results/w0-round3/final-1b82645640-release-final/optimizer-live.json) |

### Earlier measurements and open targets

- The frozen M3 held-out evaluation measured 11/17 correct mappings and 7/9 correct steps for the heuristic, and 17/17 and 9/9 for each model, on four synthetic events and three runbooks. The heuristic missed one required approval. See the separate post-fix approval replay for the changed policy. [Blind M3 results](../loadtest/results/llm-eval-92f0e5c06bd9/final/summary.md) · [Approval replay](../loadtest/results/llm-eval-92f0e5c06bd9/approval-replay.json)
- The earlier Apple M4 Pro saturation pair reconciles each run's offered, confirmed, pending and receiver counts separately. [Saturation evidence](SCALING.md#1000-keys-at-5600s-1-vs-2-intake-consumers-apple-m4-pro)
- The final acceptance does not measure paired assist-on/off p99 or validate provider logs for the dedicated alert-path target. These targets remain NOT_RUN here.
<!-- w0-release-scores-end -->

<!-- w0-release-delivery-tail-start -->
### Normal delivery tail breakdown

The preregistered <5 s target remains **FAIL**. The Apple M4 Pro trial measured normal receiver p99 **5.738 s (2,000 received / 2,000 expected)**. The stage values are medians within the 20 slowest normal receipts from that trial. [Outcome and stage evidence](../loadtest/results/w2-delivery-finalize-1891028.json)

| Stage | Tail median (ms) |
| --- | ---: |
| Offered event → durable intake | 13.554 |
| Durable intake → event processed | 237.149 |
| Event processed → first delivery attempt | 5,476.168 |
| First delivery attempt → receiver receipt | 19.847 |
| Receiver receipt → attempt finalized | 12.285 |

The observed delay concentrates before the first delivery attempt. One transaction claims up to 25 sends from a single team at its selected priority. A burst of 2,000 same-team, same-priority sends therefore needs at least 80 claims. The batch-size A/B belongs to a separate experiment. [Claim logic](../backend/alerting/delivery/fair.py)
<!-- w0-release-delivery-tail-end -->
