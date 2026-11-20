# DEV4 — Notifications & routing (recipients, outbox, delivery, retries, live UI channel)

**Branch:** `feat/delivery` · **Layers:** L5 routing + reliable delivery, runbook rendering per channel, SSE
**You own:** `backend/alerting/routing/`, `delivery/`, `runbooks/match.py`, `runbooks/render.py`, `api/routes_notifications.py`, `api/routes_stream.py`, `mocks/fake_telecom/`, `tests/delivery/`, `db/migrations/3xx_*.sql`
**Read first:** BRIEF §1–3, CONTRACT §2 (notification id), §4 (teams/runbooks), §6, §8, §9 (your rows), §10

## Mission
Get each incident to the right people on the right channel, with no spam, and never lose a notification when a channel is down. Make the UI live (SSE). Show runbook steps filled with real values, in the form that fits each channel.

## Tasks (★ = needed for G1)

### By G1 (Sat 00:00)
1. ★ **Directory** `routing/directory.py`, built from `config/files.directory()` (DEV2's loader later).
   - Resolve `user:x`, `team:x` (all members; for `ui` the recipient stays `team:x`), and `oncall:x` (primary; secondary for escalation).
   - Also expose contacts, approvers, and preferences with the team/default fallback.
2. ★ **`routing.plan()` (pure, test-first)**, signature in CONTRACT §6:
   - **Recipients:** expand the route targets.
   - **Channels:** explicit, or from preferences. The first matching row wins; `when` is evaluated in the **user's timezone** with `clock`, window `[start, end)`. A critical alert always gets at least one channel.
   - **Render per channel** (CONTRACT §8): `short` ≤ 160 chars + link `http://localhost:5173/a/<id>`; email = full body + runbook steps; phone = `short` + "press 1 to acknowledge".
   - **Dedup key:** `dedup_key = f"{alert.id}:{kind}:{recipient}:{channel}:{notify_epoch}"`.
   - **Runbook:** return `plan.runbook_id` (task 6).
   - Tests: Anna 10:00 → SMS, 21:00 → email, 08:00 → SMS, 20:00 → email; Settlements → sam phone + SMS + `team:settlements` UI; Rates → rita email + UI.
3. ★ **fake-telecom** `mocks/fake_telecom/` (FastAPI :8100):
   - `POST /sms` and `POST /call`: dedup by `Idempotency-Key`, record `received_at` and `X-Gen-Sent-At`.
   - `POST /mode {ok|down|503|slow|drop_response}`.
   - `GET /messages` returns the log as JSON.
   - `GET /` is a phone-like page per number with a reply box that forwards to `POST {API}/v1/inbound/sms`.
4. ★ **Providers + delivery worker:**
   - Providers in `delivery/providers/{email,telecom,ui}.py`:
     - Email: SMTP to Mailpit with headers `X-Notification-Id` and `X-Gen-Sent-At`.
     - SMS/phone: httpx to fake-telecom. A 3 s timeout is `unknown`; 5xx or connection refused is `retryable`; 4xx is `permanent`.
     - UI: delivered on insert.
     - A real provider (e.g. Twilio) must be **one new file**.
   - Worker `python -m alerting.delivery.worker`:
     - Claim due rows with `SKIP LOCKED`, set `sending` with `locked_until = now() + 30s`, and commit.
     - **Send outside the transaction**, then record the attempt and the new state.
     - Backoff `min(60, 2^n) ± 20 %`; honour `retry_after`; after 8 attempts or 15 minutes → `failed`.
     - Per-channel breaker. `NOTIFY notifications` after each state change.
   - An expired lease is re-claimed and resent with the **same** `Idempotency-Key`.
5. ★ **SSE** `GET /v1/stream?team=`:
   - One `LISTEN` connection per API process on `alerts, notifications, actions, schema_issues, config`.
   - Each subscriber gets its own queue with a team filter, a 15 s heartbeat, and `stats.tick` every 2 s (CONTRACT §10).
   - Agree the NOTIFY payloads with DEV3 in 5 minutes.
   - Also `GET /v1/alerts/{id}/notifications` (with attempts) and `GET /v1/notifications?state=`.

### By G2 (Sat 04:00)
6. **Runbook match + render (pure)** `runbooks/match.py`, `runbooks/render.py`:
   - Match by `rules[]`, `matches`, or a yaml-steps runbook's own `matches`.
   - `render(runbook, fields) -> steps`:
     - yaml-steps → check / action / if;
     - Markdown numbered or checkbox lines → manual steps, with "approval" wording → `approval: required`;
     - text → one manual step + `can_structure: true`;
     - url → a link.
   - Variables are filled with `templates.render`. DEV3's API and DEV2's structuring use the same step shape.
7. **Escalation + approval requests:** `escalation` and `approval_request` kinds in `plan()` (to the target team's on-call and to the approvers).
8. **Reply-to-approve** `POST /v1/inbound/{channel} {from, body}`:
   - Parse `YES|NO <code>` and map the sender's contact to a user.
   - Call DEV3's approve/reject function, which checks the approver, code, expiry and single use.

### By G3 (Sat 07:30)
9. **Channel fallback:** for a critical notification whose breaker stays open for more than 30 s, create a `fallback` on the next preferred channel (set `fallback_of`) and keep retrying the original.
10. **Throttle per recipient:** rule `throttle:` → the worker passes `recent_sends`, and `plan()` emits a `throttle_summary`.

## Acceptance
- Golden `settlements-positive` (notifications), `rates-positive`, all `anna-*`, `settlements-runbook-rendered`, `channel-down-no-loss` (0 lost).
- `kill -9` a delivery worker mid-send → re-sent with the same key (fake-telecom shows 1 unique message).
- Two browsers update live without refresh.

## Pitfalls
Never make HTTP calls inside a DB transaction: claim, commit, send, record. A timeout is `unknown`, not failed. Anna's window is in **her** timezone; use `clock_for_event`.

## Prompt for your coding agent
> Read `AGENTS.md`, `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DEV4-notifications.md` fully. You are DEV4 (notifications and routing) on branch `feat/delivery`. Work only inside your own directories. Never edit shared files; if one needs a change, stop and tell me. Do tasks 1–2 (directory and the pure `routing.plan`) test-first against `config/teams.yaml`, `config/rules.yaml` and the Anna/Settlements/Rates cases in `fixtures/golden/expectations.yaml`. Then build fake-telecom, the providers with the delivery worker, and SSE. Commit after each task, run `make test`, and push to `feat/delivery`. Python 3.12, psycopg 3 async, httpx. Report what you ran and what you didn't.
