# Gate record

Status: **PASS / FAIL / NOT_RUN** only. Record who ran it, when, and the evidence (command, screenshot, result file). Yi signs off each gate. A FAIL is recorded as FAIL, never changed to NOT_RUN.

<!-- w0-round3-start -->
## Round 3 post-merge checks

Each row uses the actual GitHub merge commit on main, local PostgreSQL, RabbitMQ and the live simulated gateway.

For #19, the first run used the shared development database. Its team-scoped SSE assertion also received global `config.changed` frames from concurrent activity. The full rerun used a dedicated PostgreSQL database on the same host with the same Git tree. Subsequent W0 runs use that database.

| PR | Main | Status | Output | Evidence |
| --- | --- | --- | --- | --- |
| [#18](https://github.com/yy31104/warsaw-hackathon-2026/pull/18) | `13a9bd4` | PASS | 478 passed in 105.68s (0:01:45) | [make test](../loadtest/results/w0-round3/merges/pr-18-13a9bd4.log) |
| [#19](https://github.com/yy31104/warsaw-hackathon-2026/pull/19) | `adcd73b` | FAIL | 1 failed, 483 passed in 101.16s (0:01:41) | [make test](../loadtest/results/w0-round3/merges/pr-19-adcd73b.log) |
| [#19](https://github.com/yy31104/warsaw-hackathon-2026/pull/19) | `adcd73b` | PASS | 484 passed in 113.49s (0:01:53) | [make test](../loadtest/results/w0-round3/merges/pr-19-adcd73b-020116.log) |
| [#20](https://github.com/yy31104/warsaw-hackathon-2026/pull/20) | `de310f6` | PASS | 484 passed in 130.04s (0:02:10) | [make test](../loadtest/results/w0-round3/merges/pr-20-de310f6.log) |
| [#21](https://github.com/yy31104/warsaw-hackathon-2026/pull/21) | `7196825` | PASS | 487 passed in 99.88s (0:01:39) | [make test](../loadtest/results/w0-round3/merges/pr-21-7196825.log) |
| [#22](https://github.com/yy31104/warsaw-hackathon-2026/pull/22) | `ae69a28` | PASS | 487 passed in 98.40s (0:01:38) | [make test](../loadtest/results/w0-round3/merges/pr-22-ae69a28.log) |
| [#24](https://github.com/yy31104/warsaw-hackathon-2026/pull/24) | `f59f41e` | PASS | 518 passed in 100.83s (0:01:40) | [make test](../loadtest/results/w0-round3/merges/pr-24-f59f41e.log) |
| [#23](https://github.com/yy31104/warsaw-hackathon-2026/pull/23) | `b42695e` | PASS | 525 passed in 102.13s (0:01:42) | [make test](../loadtest/results/w0-round3/merges/pr-23-b42695e.log) |
| [#25](https://github.com/yy31104/warsaw-hackathon-2026/pull/25) | `43aa4fe` | PASS | 542 passed in 102.37s (0:01:42) | [make test](../loadtest/results/w0-round3/merges/pr-25-43aa4fe.log) |
| [#26](https://github.com/yy31104/warsaw-hackathon-2026/pull/26) | `8d698dc` | PASS | 572 passed in 102.01s (0:01:42) | [make test](../loadtest/results/w0-round3/merges/pr-26-8d698dc.log) |
| [#28](https://github.com/yy31104/warsaw-hackathon-2026/pull/28) | `79e5b8f` | PASS | 572 passed in 106.61s (0:01:46) | [make test](../loadtest/results/w0-round3/merges/pr-28-79e5b8f.log) |
| [#29](https://github.com/yy31104/warsaw-hackathon-2026/pull/29) | `10c398e` | PASS | 580 passed in 109.30s (0:01:49) | [make test](../loadtest/results/w0-round3/merges/pr-29-10c398e.log) |
| [#31](https://github.com/yy31104/warsaw-hackathon-2026/pull/31) | `dfa2a8a` | PASS | 580 passed in 110.21s (0:01:50) | [make test](../loadtest/results/w0-round3/merges/pr-31-dfa2a8a.log) |
| [#32](https://github.com/yy31104/warsaw-hackathon-2026/pull/32) | `d7a6884` | PASS | 580 passed in 103.50s (0:01:43) | [make test](../loadtest/results/w0-round3/merges/pr-32-d7a6884.log) |
| [#35](https://github.com/yy31104/warsaw-hackathon-2026/pull/35) | `b84bd4e` | PASS | 580 passed in 104.39s (0:01:44) | [make test](../loadtest/results/w0-round3/merges/pr-35-b84bd4e.log) |
| [#36](https://github.com/yy31104/warsaw-hackathon-2026/pull/36) | `20e61d9` | PASS | 591 passed in 122.98s (0:02:02) | [make test](../loadtest/results/w0-round3/merges/pr-36-20e61d9.log) |
| [#34](https://github.com/yy31104/warsaw-hackathon-2026/pull/34) | `a392465` | PASS | 591 passed in 116.01s (0:01:56) | [make test](../loadtest/results/w0-round3/merges/pr-34-a392465.log) |
| [#39](https://github.com/yy31104/warsaw-hackathon-2026/pull/39) | `345230d` | PASS | 591 passed in 115.45s (0:01:55) | [make test](../loadtest/results/w0-round3/merges/pr-39-345230d.log) |
| [#33](https://github.com/yy31104/warsaw-hackathon-2026/pull/33) | `e9901e5` | PASS | 652 passed in 125.25s (0:02:05) | [make test](../loadtest/results/w0-round3/merges/pr-33-e9901e5.log) |
| [#30](https://github.com/yy31104/warsaw-hackathon-2026/pull/30) | `87ddbe5` | PASS | 731 passed in 130.64s (0:02:10) | [make test](../loadtest/results/w0-round3/merges/pr-30-87ddbe5.log) |
| [#37](https://github.com/yy31104/warsaw-hackathon-2026/pull/37) | `a3b95f2` | PASS | 736 passed in 138.61s (0:02:18) | [make test](../loadtest/results/w0-round3/merges/pr-37-a3b95f2.log) |
| [#40](https://github.com/yy31104/warsaw-hackathon-2026/pull/40) | `94e4cba` | PASS | 736 passed in 150.93s (0:02:30) | [make test](../loadtest/results/w0-round3/merges/pr-40-94e4cba.log) |
| [#41](https://github.com/yy31104/warsaw-hackathon-2026/pull/41) | `693246e` | PASS | 736 passed in 131.77s (0:02:11) | [make test](../loadtest/results/w0-round3/merges/pr-41-693246e.log) |
| [#42](https://github.com/yy31104/warsaw-hackathon-2026/pull/42) | `d63a7c5` | PASS | 750 passed in 143.05s (0:02:23) | [make test](../loadtest/results/w0-round3/merges/pr-42-d63a7c5.log) |
| [#44](https://github.com/yy31104/warsaw-hackathon-2026/pull/44) | `356ea3c` | FAIL | 1 failed, 777 passed in 148.62s (0:02:28) | [make test](../loadtest/results/w0-round3/merges/pr-44-356ea3c.log) |
| [#44](https://github.com/yy31104/warsaw-hackathon-2026/pull/44) | `356ea3c` | PASS | 778 passed in 140.59s (0:02:20) | [make test](../loadtest/results/w0-round3/merges/pr-44-356ea3c-060844.log) |
| [#43](https://github.com/yy31104/warsaw-hackathon-2026/pull/43) | `1a9d0dd` | PASS | 798 passed in 141.93s (0:02:21) | [make test](../loadtest/results/w0-round3/merges/pr-43-1a9d0dd.log) |
| [#45](https://github.com/yy31104/warsaw-hackathon-2026/pull/45) | `09d577a` | PASS | 798 passed in 139.48s (0:02:19) | [make test](../loadtest/results/w0-round3/merges/pr-45-09d577a.log) |
<!-- w0-round3-end -->

<!-- w0-console-start -->
## Integrated frontend verification

| PR | Main | Frontend build | Console browser | Evidence |
| --- | --- | --- | --- | --- |
| [#30](https://github.com/yy31104/warsaw-hackathon-2026/pull/30) | `87ddbe5` | PASS | PASS (10/10 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-30-87ddbe5/receipt.json) |
| [#37](https://github.com/yy31104/warsaw-hackathon-2026/pull/37) | `a3b95f2` | PASS | PASS (10/10 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-37-a3b95f2/receipt.json) |
| [#40](https://github.com/yy31104/warsaw-hackathon-2026/pull/40) | `94e4cba` | PASS | PASS (13/13 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-40-94e4cba/receipt.json) |
| [#41](https://github.com/yy31104/warsaw-hackathon-2026/pull/41) | `693246e` | PASS | PASS (13/13 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-41-693246e/receipt.json) |
| [#42](https://github.com/yy31104/warsaw-hackathon-2026/pull/42) | `d63a7c5` | PASS | PASS (13/13 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-42-d63a7c5/receipt.json) |
| [#44](https://github.com/yy31104/warsaw-hackathon-2026/pull/44) | `356ea3c` | PASS | PASS (13/13 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-44-356ea3c/receipt.json) |
| [#43](https://github.com/yy31104/warsaw-hackathon-2026/pull/43) | `1a9d0dd` | PASS | PASS (13/13 completed) | [Command receipts](../loadtest/results/w0-round3/console/pr-43-1a9d0dd/receipt.json) |
<!-- w0-console-end -->

## G0 — contract (Fri 21:00)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| All four read BRIEF + CONTRACT + own handoff | NOT_RUN | | |
| `make infra` + `make test` green on all four laptops | NOT_RUN | | |
| Golden expectations checked line by line against the printed task (Yi + daizhongtian) | NOT_RUN | | |
| Mentor answers: deadline, Lockbox path, 2.4 meaning | NOT_RUN | | |

## G1 — vertical slice (Sat 00:00)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| RabbitMQ → PostgreSQL inbox → rule worker → outbox → loopback HTTP receiver | PASS | Yi · 2026-11-20 | Production code from integrated main `accfd72`. `cd backend && INGEST_TEST_RABBITMQ_URL=amqp://localhost:5672/ uv run pytest -q tests/processing/test_broker_e2e.py`. One RabbitMQ publish produced one alert, two HTTP receipts (phone and SMS), one UI delivery and two successful provider attempts. With the broker enabled, the full suite passed 131 tests. |
| RabbitMQ `workflow_failed.json` → fake-telecom shows phone + SMS to sam | PASS | Yi · 2026-11-20 | Production services from main `b45e604`, test `49b1d68`. `cd backend && INGEST_TEST_RABBITMQ_URL=amqp://localhost:5672/ uv run pytest -q tests/processing/test_broker_e2e.py`. The production consumer wrote the inbox row. The rule worker created one alert, two pending phone/SMS notifications and a delivered UI notification. `/messages` returned one receipt for each expected notification ID, channel and on-call number. Both provider attempts were recorded as delivered. |
| Same event → UI card appears without refresh (SSE) | NOT_RUN | | |
| Branches merged into `main`, `make test` green | NOT_RUN | | |

## G2 — all example rules (Sat 04:00)
| Golden case group | Status | Who / when | Evidence |
|---|---|---|---|
| settlements-* (4) | NOT_RUN | | |
| rates-* (6) | NOT_RUN | | |
| anna-* + alertmanager-auto-resolve (7) | NOT_RUN | | |
| pricing-digest | NOT_RUN | | |
| retransmission-dedup, storm-500-one-alert | NOT_RUN | | |
| unknown-schema-visible | NOT_RUN | | |
| approval-required / wrong-person / ok | NOT_RUN | | |
| Three runbook formats render usable steps | NOT_RUN | | |

## G3 — feature freeze (Sat 07:30)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| channel-down-no-loss (0 lost) | NOT_RUN | | |
| Worker kill -9 recovery (ingest + delivery) | NOT_RUN | | |
| Private holdout (Marlon): unseen schemas onboarded without code (time each) | NOT_RUN | | |
| Private holdout (Marlon): unseen runbook → usable steps | NOT_RUN | | |
| Load test raw results committed (1 vs 2 workers, p50/p99 with N) | NOT_RUN | | |
| Fresh clone → first alert: commands + minutes (other laptop) | NOT_RUN | | |
| Rule preview leaves DB unchanged | NOT_RUN | | |
| Unaided UI tasks by a teammate (time) | NOT_RUN | | |

## G4 — safe submission (Sat 10:30)
| Check | Status | Who / when | Evidence |
|---|---|---|---|
| README: run steps, architecture, what is mocked, measured results, AI use + privacy | NOT_RUN | | |
| Video ≤ 5:00 uploaded to Lockbox `All Teams/<team>` | NOT_RUN | | |
| Text file with repo link uploaded | NOT_RUN | | |

## Golden run

On 2026-11-20 23:33 UTC, the pre-merge run at `e28da39` (including `main` at `47f7a31`) recorded **32 PASS, 0 FAIL, 0 NOT_RUN**. `tools/test_golden_isolated.ps1` started a fresh Compose project with RabbitMQ, the pricing file input, Mailpit and fake-telecom. The digest case waited for its five-minute delivery interval.

The same code passed 470 backend tests with PostgreSQL and RabbitMQ. The database upgrade was also run twice over persisted data. A volume initialized from the `47f7a31` migration files started the upgraded API, returned five rules and retained its audit record. The frontend passed 10 tests, ESLint and the real-API production build.

| Case | Status | Evidence |
|---|---|---|
| settlements-positive | PASS | All declared checks passed |
| settlements-negative-env | PASS | All declared checks passed |
| settlements-negative-workflow | PASS | All declared checks passed |
| settlements-runbook-rendered | PASS | All declared checks passed |
| rates-positive | PASS | All declared checks passed |
| rates-boundary-equal | PASS | All declared checks passed |
| rates-other-desk | PASS | All declared checks passed |
| rates-schema-glob-v3 | PASS | All declared checks passed |
| rates-not-rejected | PASS | All declared checks passed |
| rates-missing-template-field | PASS | All declared checks passed |
| anna-daytime-sms | PASS | All declared checks passed |
| anna-evening-email | PASS | All declared checks passed |
| anna-boundary-0800 | PASS | All declared checks passed |
| anna-boundary-2000 | PASS | All declared checks passed |
| anna-not-critical | PASS | All declared checks passed |
| anna-other-service | PASS | All declared checks passed |
| alertmanager-auto-resolve | PASS | All declared checks passed |
| pricing-digest | PASS | All declared checks passed |
| retransmission-dedup | PASS | All declared checks passed |
| storm-500-one-alert | PASS | All declared checks passed |
| unknown-schema-visible | PASS | All declared checks passed |
| approval-required | PASS | All declared checks passed |
| approval-wrong-person | PASS | All declared checks passed |
| approval-ok | PASS | All declared checks passed |
| channel-down-no-loss | PASS | All declared checks passed |
| stale-recovery-keeps-newer-episode | PASS | All declared checks passed |
| poison-event-isolated | PASS | All declared checks passed |
| inbound-approval-needs-provider-secret | PASS | All declared checks passed |
| inbound-approval-brute-force-locked | PASS | All declared checks passed |
| verify-unit-change | PASS | All declared checks passed |
| rule-safety-counterexample | PASS | All declared checks passed |
| rule-safety-ui-only-is-valid | PASS | All declared checks passed |

<!-- w0-watcher-qa-start -->
## Parser watcher QA on `09d577a`

Local QA reported **797/798** in a concurrent run. The parser watcher test timed out. Its 250 ms pause before the invalid edit is sensitive to subscription timing under parallel load. The original failing terminal log was unavailable in the release worktree.

Five isolated runs of the exact test passed **5/5** on `09d577ac74bcc04ad899bd36ed754e9415e00c5d`. GitHub CI on the same SHA passed **798/798**. The test verifies that the earlier parser stays active after a rejected edit and a corrected edit is committed. W0 classifies the reported timeout as a test timing flake under concurrent load. [Five isolated runs](../loadtest/results/w0-round3/watcher-09d577a/isolated.log) · [CI excerpt](../loadtest/results/w0-round3/watcher-09d577a/github-ci.log) · [GitHub job](https://github.com/yy31104/warsaw-hackathon-2026/actions/runs/44870059138/job/136742336303)
<!-- w0-watcher-qa-end -->

<!-- w0-final-acceptance-start -->
## Final acceptance

Fresh isolated Compose project on integrated main `1b82645640736f48f31da6f41945cfae8cd93b0b`. Started 2026-11-21T05:56:00.413640+00:00. Finished 2026-11-21T05:57:50.164596+00:00. Times are UTC. Backend, golden and input-variation evidence was measured on `dd9d4834216f63ded7dabd130ea6b58ea62c1d3f`. The following changes were limited to frontend presentation and browser assertions. The command receipt lists every changed path.

[Exact commands, exit codes and durations](../loadtest/results/w0-round3/final-1b82645640-release-final/suite.json) · [Innovation and release browser commands](../loadtest/results/w0-round3/final-1b82645640-release-final/innovation-commands.json)

| Gate | Status | Observation | Evidence |
| --- | --- | --- | --- |
| Fresh clone through first API-visible alert | PASS | Clone to first alert 24.906 s. Compose start to first alert 17.045 s. README sample from clone 25.079 s. Docker layers were cached; project volumes started empty. | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/first-alert.json) |
| Backend · make test | PASS | 798 passed in 151.00s (0:02:30) | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/backend-tests.log) |
| Frontend · npm test | PASS | 19 passed (19) | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/frontend-tests.log) |
| Frontend · lint | PASS | npm run lint | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/frontend-lint.log) |
| Frontend · production build | PASS | npm run build | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/frontend-build.log) |
| Golden cases | PASS | 32 PASS, 0 FAIL, 0 NOT_RUN | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/golden.log) |
| Browser walkthrough | PASS | A · live critical incident and acknowledge PASS, B · Lee approves persisted retry in a separate context PASS, Timeline · one plain row per delivery and raw history in More details PASS, C and F · unknown JSON verified and routed with assist off PASS, D · rule typo suggestion and versioned publish PASS, E · structured runbook validation blocks publish until fixed PASS, On call · preference sliders persist PASS, Health · live counters and dated benchmark provenance PASS, Now · one fallback tile or four live response tiles PASS, Now · four tiles with an example response and a missing sample PASS, Intro · auto-enter and session replay PASS, Screenshots · four layouts across eight views PASS, Network · genuine API, no fixtures or browser errors PASS | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/browser/result.json) |
| Browser walkthrough · first attempt | FAIL | The shared simulator retained golden receipts after the app reset reused alert IDs. | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/browser-first-attempt.log) |
| Browser walkthrough · clean retry before harness fix | FAIL | The original test expected dark screenshots in default light sessions. PR #46 corrected the test. The browser check above uses the amended SHA. | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/browser-original-clean-retry.log) |
| Browser walkthrough · interim screenshot timeout | FAIL | The screenshot sweep on the prior frontend SHA aborted after a request timeout. The final SHA was tested separately. | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/browser-interim-timeout.log) |
| Release browser checklist | PASS | 8/8 checks passed on `1b82645` before the final clean-stack run. | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/innovation-browser/result.json) |
| Golden expectations integrity | PASS | Git blob `e8ac783e4f1efae0a9d845d0e60dead75a7acb00` | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/suite.json) |
| M16 measurements | PASS | 8 deviations plus the control. The table records behavior independently from harness success. | [Evidence](../loadtest/results/w0-round3/final-1b82645640-release-final/m16.json) |

### README input variations

| Change | HTTP | Stored / decision | Observed route |
| --- | --- | --- | --- |
| README baseline | 202 | processed / NEW_ALERT | anna / sms |
| Renamed service field | 202 | processed / UNROUTED | None observed |
| Upper-case severity | 202 | processed / NEW_ALERT | anna / sms |
| Epoch seconds | 202 | processed / NEW_ALERT | anna / sms |
| Extra nesting | 202 | needs_review / NEEDS_REVIEW | None observed |
| CSV with dotted headers | 202 | needs_review / NEEDS_REVIEW | None observed |
| Nested XML | 202 | processed / NO_MATCH | None observed |
| Timestamp without timezone | 202 | processed / NEW_ALERT | anna / sms |
| Typo in a rule field | 202 | Draft check HTTP 200, publish HTTP 200; cloned rule decision before/after the typo NEW_ALERT / NO_MATCH. The existing Anna rule remains active. | anna / sms |

[Requests, responses, rule checks and decisions](../loadtest/results/w0-round3/final-1b82645640-release-final/m16.json)
### Release decision

**GO.** The final acceptance gates passed and W0 found no open P0 blocker. The normal delivery p99 target remains a scorecard FAIL and does not gate this release. [Scorecard](SCORECARD.md)

<!-- w0-final-acceptance-end -->
