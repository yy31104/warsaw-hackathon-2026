# Handoff — REVIEW & PRESENT (joins at G3, Sat ~07:30; earlier if free)

**Branch:** `review/present` (docs, video assets). Fixes go as small PRs to the owner's branch, or directly to `main` with Yi's OK after G3.
**Read first:** BRIEF (all), `docs/GATES.md`, the task paper (all pages). Skim CONTRACT §1–2.

## Why you come in last
You haven't seen the code, which is exactly what the judges will be like. Your fresh eyes find what the three builders can't: confusing screens, unclear README steps, untested claims. You also own the story and the video, which is the primary judging artifact.

## Tasks (in order)

### 1. Fresh-eyes product check (~45 min)
Without help, try these on the running system (`main`). Time each one and note every point where you hesitate:
- From a **fresh clone** on your laptop, follow only the README → first alert. Record the number of commands and minutes.
- In the UI: change Anna's SMS window to 09:00–18:00; create a rule "trade.rejected on desk fx with notional > 5,000,000 → email team rates"; ack and resolve an alert; run a runbook retry; approve an escalation as `lee`.
- Find out *why* an alert did or didn't notify someone (the explain view).
→ Write the findings in `docs/REVIEW_FINDINGS.md` (severity, screen, what happened, suggested fix). Hand them to the owner, or fix small copy/UI issues yourself.

### 2. Hidden-input rehearsal (~30 min)
Yi gives you the private holdout folder (not in the repo). Send each unseen schema and runbook through the UI **without code changes**, exactly like the judges will. Record PASS / FAIL, minutes to the first correct alert, and LLM-suggested vs correct fields (n/N) in GATES.md (G3 rows). FAIL is a valid, useful result.

### 3. Evidence audit (~20 min)
For every number and claim in the README / video script, find the script or result file that produced it (`loadtest/results/`, GATES.md). Anything without a source is removed or marked "not measured". Check that "what is mocked" is complete (fake-telecom = SMS/phone gateway, Mailpit = SMTP inbox, mock-ops = workflow/ops targets, simulated auth via `X-User`).

### 4. README for judges (~30 min)
Rewrite `README.md` top to bottom for a judge. Keep it short:
- one-paragraph pitch;
- architecture diagram (BRIEF §5);
- quick start (the commands you actually ran in step 1);
- feature → task-requirement map (2.1–2.6);
- measured results table (with N and machine spec);
- what is mocked;
- AI use in development and in the product + how privacy/security were handled (redaction, no real data, keys local only, LLM never on the alert path);
- known limitations / next steps (single Postgres as the bottleneck and single point of failure; real providers; RBAC).

### 5. Video (≤ 5:00) (~90 min including retakes)
Follow BRIEF §9. Record at 1440×900, large fonts, one narrator. Show real runs (fake-telecom phone page, Mailpit inbox, live counter, approval by SMS reply, unseen-schema onboarding with a stopwatch, measured numbers). No number on screen without a source. Upload to Lockbox `All Teams/<team>` together with a text file containing the repo link.

### 6. Q&A prep for the top-5 final (~20 min)
One page `docs/QA.md`: bottleneck (Postgres write path and the hot alert row → batch claims + pre-aggregation; next step is partitioning or sharding by team), single point of failure (Postgres; replica + failover in production), exactly-once (we give durable intake, idempotent effects inside the DB, and idempotency keys outward; a timeout is "unknown", not "failed"), why no Redis/Kafka, how a real SMS provider is added (one class), how the LLM is kept safe (drafts only, human confirms, redaction, fallback).

## Done when
`docs/REVIEW_FINDINGS.md` exists, the G3 holdout rows and G4 rows in GATES.md are filled, the README is judge-ready, and the video plus link file are uploaded (confirmed by seeing them in Lockbox).
