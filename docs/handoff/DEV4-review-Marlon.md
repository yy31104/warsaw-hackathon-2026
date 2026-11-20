# DEV4 · Marlon Mejia — Review & presentation (joins at G3, Sat ~07:30; earlier if free)

**Branch:** `feat/review-marlon` · **GitHub:** `MarlonMejia15` (accept the repo invite first)
**You own:** `docs/REVIEW_FINDINGS.md`, `docs/QA.md`, the judge-facing `README.md` (from G3, together with Yi), and the video. Code fixes go as small PRs to the owner's branch, or to `main` with Yi's OK after G3.
**Read first:** BRIEF (all of it), `docs/GATES.md`, the printed task (all pages). Skim CONTRACT §1–2.

## Why you come in last
You haven't seen the code, and neither will the judges. Your fresh eyes catch what the three builders can't: confusing screens, unclear README steps, claims nobody tested. You also own the story and the video, which is what we are mainly judged on.

## Tasks (in order)

### 1. Fresh-eyes product check (~45 min)
Use the running system on `main`, with no help. Time each step and note every hesitation.
- Fresh clone on your laptop → follow only the README → first alert. Count the commands and the minutes.
- In the UI:
  - change Anna's SMS window to 09:00–18:00;
  - create a rule "trade.rejected on desk fx with notional > 5,000,000 → email team rates";
  - ack and resolve an incident;
  - run a runbook retry;
  - approve an escalation as `lee`.
- Find out *why* an incident did or didn't notify someone (the explain view).

Write the findings in `docs/REVIEW_FINDINGS.md`: severity, screen, what happened, suggested fix. Send each finding to the owner (Yi = backend, daizhongtian = data/API, zhaoqinghe = UI), or fix small copy issues yourself.

### 2. Hidden-input rehearsal (~30 min)
Yi gives you the **private holdout** folder. It isn't in the repo, and nobody else should see it. It holds unseen event formats plus an unseen runbook, simulating what the judges will bring.
- Send each item through the UI **without code changes**.
- Record in `GATES.md` (G3 rows): PASS / FAIL, minutes to the first correct alert, LLM-suggested vs correct fields (n/N).
- A FAIL is a valid, useful result. Add one unseen format of your own.

### 3. Evidence audit (~20 min)
- Every number and claim in the README and video script needs a source: a script or result file (`loadtest/results/`, `GATES.md`). Remove anything without one, or mark it "not measured".
- Check that "what is mocked" is complete:
  - fake-telecom = SMS/phone gateway;
  - Mailpit = SMTP inbox;
  - mock-ops = workflow/ops targets;
  - simulated auth via `X-User`.

### 4. README for judges (~30 min)
Keep it short:
- a one-paragraph pitch;
- the architecture (BRIEF §3 seams);
- quick start (the commands you actually ran in step 1);
- feature → requirement map (task 2.1–2.6);
- a measured-results table with N and the machine spec;
- what is mocked;
- AI use in development and in the product, plus privacy/security (redaction, synthetic data only, keys kept local, LLM never on the alert path);
- limitations and next steps (single Postgres = bottleneck and single point of failure; real providers; RBAC).

### 5. Video, ≤ 5:00 (~90 min with retakes)
- Follow the storyboard in BRIEF §7.
- Record at 1440×900, large fonts, one narrator.
- Show real runs only: the fake-telecom phone page, the Mailpit inbox, the live counter, approval by SMS reply, unseen-schema onboarding with a stopwatch, measured numbers.
- No number on screen without a source.
- Upload to Lockbox `All Teams/<team>` together with a text file containing the repo link.

### 6. Q&A prep for the top-5 final (~20 min)
One page, `docs/QA.md`:
- **Bottleneck:** Postgres writes and the hot incident row → batch claim + pre-aggregation. Next step: partition or shard by team.
- **Single point of failure:** Postgres → replica + failover.
- **Exactly-once:** durable intake, idempotent effects in the DB, idempotency keys on outbound calls. A timeout means "unknown", not "failed".
- **Why no Redis or Kafka.**
- **Adding a real SMS provider:** one class.
- **LLM safety:** it only drafts, a human confirms, data is redacted, and there's a fallback.

## Done when
`REVIEW_FINDINGS.md` exists, the G3 holdout rows and the G4 rows in `GATES.md` are filled, the README is judge-ready, and the video + link file are uploaded. Confirm it by seeing them in Lockbox.

## Round 2 additions — adversarial checks (run them like a judge would)
For each one, record PASS / FAIL / NOT_RUN plus the evidence:
1. **Poison event:** one malformed event in a batch → it is quarantined with a reason; the others complete; the worker keeps running.
2. **Stale recovery:** episode T1 resolves late, after episode T2 started → the alert stays open; T2's own resolve closes it.
3. **Approval abuse:** inbound SMS without the provider secret → 401; 5 wrong codes → locked; a reused code → rejected.
4. **Same business, different unit:** a notional sent in cents → the verify page shows quiet, with the deciding field; without the transform it warns.
5. **Rule safety:** a draft rule routing critical alerts to a non-existent on-call → a counterexample is shown before publishing.
6. **Provider outage:** fake-telecom down for 30 s → outbox reconciliation shows lost = 0.
7. **Time edge:** Anna at 07:59 / 08:00 / 19:59 / 20:00 and on the DST weekend (25 Oct 2026, Europe/Warsaw).
8. **Unseen format:** time from paste to the first correct alert, and LLM-suggested vs correct fields (n/N).
