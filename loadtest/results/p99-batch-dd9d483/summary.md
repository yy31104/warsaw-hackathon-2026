# Delivery batch 25 vs 50 · matched end-to-end trial (config only)

**Result: normal receiver p99 <5 s · FAIL. Batch 50 did not beat the matched batch-25 baseline.** No application code changed. Only `DELIVERY_BATCH` was set, through the Compose environment. The verdict is the same for `dd9d483` (images) and `b4a32c2` (main when this was written), because `backend/`, `docker-compose.yml`, `loadtest/*.py` and `fixtures/` are identical in both.

## Method

- Host: Apple M4 Pro (macOS 27.2, Docker VM 14 CPUs), 2026-11-21 06:54–07:08 CET. Each run folder holds `machine.json` (images, scripts SHA-256) and `host.json` (load average, running containers, delivery-worker env read from the live container, 5 s samples of load and other processes during the run).
- Workload: the pre-registered W2 end-to-end trial. 700 normal + 2 critical events/s for 60 s, 1,000 normal keys, 20 s drain, 2 intake consumers, 4 normal rule workers, 1 critical rule worker, 1 delivery worker. Every run used workload SHA-256 `5804a2c054511b70324d863286d262bcbb02b3c8949b1a6feee529841dc5b4be`, the same as the original `cc5bd7c` trial.
- Command per run: `DELIVERY_BATCH=<b> DELIVERY_CONCURRENCY=16 python3.12 loadtest/run_saturation.py --rates 700 --keys 1000 --workers 4 --duration 60 --critical-rate 2 --delivery-workers 1 --ingest-workers 2 --drain-seconds 20 --image-project af-p99-img`. Exact commands: `*/m11-*/commands.json`, `*/reproduce.txt`. Wrapper scripts: [`tools/`](tools/) (`WORKDIR=<folder holding the clone> tools/run_seq.sh r2-b25:25:16 r3-a50:50:16 …`). Before commit, local home and scratch paths in the evidence were replaced with `~`, `<repo>` or `<workdir>`. Names of containers and Compose projects that do not belong to this project were replaced with `<other>`. Their images, counts and CPU samples are unchanged. The `observations.json.gz` files and their checksums are untouched.
- Order: B25, A50, A50, B25 (r2–r5), with a fresh Compose project and volumes for each run. Each run started only after the host had run no pytest, Playwright, golden runner or docker build/compose from other sessions for 30 s and the 1-minute load average was below 9.
- Analysis: [`tools/analyze.py`](tools/analyze.py) reads `summary.json` and `observations.json.gz` from each run. It matches each notification to its triggering event by alert step and generation millisecond. On the original `cc5bd7c` after-trial it reproduces the published slowest-20 medians: 13.554 / 237.149 / 5,476.168 / 19.847 ms.

## Environment

This host was busier than the original trial. Compose projects `alertfunnel-demo` and `alertfunnel-local-review` (14 containers each) ran idle for the whole session and were not touched. The original trial did not have them. The 1-minute load average inside the trial windows was 7.4–9.5. The matched batch-25 baseline therefore measured **7.167 and 6.722 s**, not the original 5.738 s. Candidate A is compared with this matched baseline.

## Runs

| Run | Batch / concurrency | Offer (CET) | Normal p50 / p95 / p99 ms (received / expected) | Critical p50 / p95 / p99 ms (received / expected) | Duplicate receipts | Events confirmed → processed | Slowest-20 normal median ms: offer→ingest / ingest→processed / processed→first claim / claim→receipt | Peak pending deliveries | Load (1 min) mean / max | Other sessions' tests or builds in window |
| --- | --- | --- | --- | --- | ---: | --- | --- | ---: | --- | --- |
| r1-b25 · **excluded** | 25 / 16 | 06:54:28–06:55:28 | 6716 / 10637 / 10826 (2,000 / 2,000) | 230 / 489 / 15411 (240 / 240) | 0 | 42,120 → 42,120 | 13 / 635 / 10170 / 15 | 1,666 | 18.1 / 20.0 | Playwright until about 20 s before the offer, then another session's image build |
| r2-b25 | 25 / 16 | 07:00:13–07:01:13 | 4249 / 6978 / **7167** (2,000 / 2,000) | 210 / 304 / 347 (240 / 240) | 0 | 42,120 → 42,120 | 8 / 139 / 7003 / 21 | 1,652 | 7.4 / 8.3 | none |
| r3-a50 | 50 / 16 | 07:02:05–07:03:05 | 4066 / 7006 / **7426** (2,000 / 2,000) | 231 / 349 / 557 (240 / 240) | 0 | 42,120 → 42,120 | 8 / 249 / 7073 / 109 | 1,830 | 7.8 / 8.6 (30 s samples) | not sampled |
| r4-a50 | 50 / 16 | 07:04:38–07:05:38 | 4180 / 6844 / **7086** (2,000 / 2,000) | 229 / 333 / 638 (240 / 240) | 0 | 42,120 → 42,120 | 8 / 244 / 6762 / 111 | 1,831 | 7.8 / 8.4 | none |
| r5-b25 | 25 / 16 | 07:06:30–07:07:30 | 3866 / 6467 / **6722** (2,000 / 2,000) | 214 / 326 / 405 (240 / 240) | 0 | 42,120 → 42,120 | 3 / 113 / 6592 / 23 | 1,636 | 9.5 / 19.7 | none |

Notes:

- **r1 is excluded.** At 06:55:27 the whole pipeline paused for about 11 s: intake, rule processing and sends all stopped. The pause overlapped another session's image build for its release stack: that frontend image was created at 06:56:19, and a `docker-buildx` process was still running at 06:57. A Playwright run ended about 20 s before the offer. The run is kept as raw evidence only.
- **r3 host record is reconstructed.** A quoting bug in the wrapper lost its pre/post/5 s snapshots and the delivery-worker env capture after the trial had finished. Its load and Compose projects come from [`compose-ls.jsonl`](compose-ls.jsonl), a separate 30 s sampler. The trial data in `r3-a50/m11-*` is complete. The env was exported exactly as for r4, whose captured worker env shows `DELIVERY_BATCH=50`.
- **r5 load spike.** The 1-minute load average jumped to 19.7 in the last 2 s of the offer and stayed there through the drain. No new Compose project appeared. The pipeline did not stall: the largest intake-to-processed time for events in the last 8 s was 679 ms. The run is kept.

## Gates (both runs of the arm must pass)

| Gate | B25 baseline (r2, r5) | Candidate A 50/16 (r3, r4) |
| --- | --- | --- |
| Normal p99 <5 s | FAIL (7.167, 6.722 s) | **FAIL** (7.426, 7.086 s) |
| Critical p99 <1 s | PASS (0.347, 0.405 s) | PASS (0.557, 0.638 s) |
| Normal receipts 2,000 / 2,000 | PASS | PASS |
| Critical receipts 240 / 240 | PASS | PASS |
| Zero duplicate or invalid receiver effects | PASS | PASS |
| No accepted-event loss (42,120 confirmed = accepted = processed, 0 pending) | PASS | PASS |
| Better than matched baseline | — | **NO**: mean p99 7.256 vs 6.945 s (×1.045). Adjacent pairs r3/r2 ×1.036, r4/r5 ×1.054. |
| Matched baseline reproduces ~5.7 s | **NO** (7.167, 6.722 s). Busier host, see Environment. | — |

Candidate B (50/24) and candidate C (two delivery workers, 25/16): **NOT_RUN**. The pre-agreed rule runs them only if 50/16 clearly beats the matched baseline.

## What the stage breakdown shows

In the 20 slowest normal receipts, nearly all of the time is still processed→first claim: 6.6–7.0 s at batch 25 and 6.8–7.1 s at batch 50. Doubling the claim size did not shorten this wait. Claim→receipt grew from about 22 ms to about 110 ms. With 16 send slots, a 50-row claim takes several rounds of sends before it finishes. Peak pending deliveries rose from about 1,640 to about 1,830. Critical p99 stayed under 1 s but rose from 0.35–0.41 s to 0.56–0.64 s. This does not support the hypothesis that the number of claim cycles (2,000 / 25 ≈ 80) sets the tail. The drain rate looks limited by time spent per sent row, not by claim overhead. Two runs per arm on one host cannot isolate that cost. Doing so would need timing inside the worker (send, per-row finalize, pool checkout), which this config-only trial did not add.
