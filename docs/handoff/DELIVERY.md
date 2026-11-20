# Handoff — DELIVERY (Backend: routing, delivery & runbooks, the "API" role)

**Branch:** `delivery/runbooks` · **Layers:** L5 routing + reliable delivery, L6 runbooks/actions/approval backend
**You own:** `backend/alerting/routing/`, `delivery/`, `runbooks/`, `assist/runbook_structure.py`, `api/routes_notifications.py`, `routes_runbooks.py`, `routes_teams.py`, `mocks/mock_ops/`, `db/migrations/2xx_*.sql`
**Read first:** BRIEF §1–2, CONTRACT §2 (notification id), §4 (teams/runbooks), §6, §8, §9 (your rows)

## Mission
Get each alert to the right people on the right channel, and never lose it when a channel is down. Then help them fix it: the runbook steps are filled with real values, safe steps run in one click, risky steps wait for an authorised human, and everything is audited.

## Tasks (in order)

### By G1 (Sat 00:00)
1. **Directory** `routing/directory.py` (from `config/files.directory()`, later DATA's loader): resolve `user:x`, `team:x` (all members; for `ui`, keep `team:x` as recipient), `oncall:x` (primary; `secondary` for escalation), contacts, approvers, and preferences with the team/default fallback.
2. **`routing.plan()` (pure, unit-tested first)** `routing/plan.py`, signature in CONTRACT §6:
   - **Recipients:** route targets → recipients.
   - **Channels:** explicit, or via `preferences` (first matching row; `when` evaluated in the **user's timezone** with `clock`, window `[start, end)`). **A critical alert always gets ≥ 1 channel.**
   - **Throttle:** apply `recent_sends`; overflow becomes a `throttle_summary`.
   - **Render per channel** (CONTRACT §8): `short` ≤ 160 chars with link `http://localhost:5173/a/<id>`; email = full body + runbook steps; phone = `short` + "press 1 to acknowledge".
   - **dedup_key:** `f"{alert.id}:{kind}:{recipient}:{channel}:{notify_epoch}"`.
   - **Runbook choice:** returns `plan.runbook_id` (task 5).
   - Tests: Anna 10:00 → SMS, 21:00 → email, 08:00 → SMS, 20:00 → email; Settlements → sam phone + sms + `team:settlements` ui; Rates → rita email + ui.
3. **Providers** `delivery/providers/{email,telecom,ui}.py` implementing `ChannelProvider`:
   - email via SMTP to Mailpit (headers `X-Notification-Id`, `X-Gen-Sent-At`);
   - sms/phone via httpx to fake-telecom `/sms` `/call` (headers `Idempotency-Key`, `X-Gen-Sent-At`; timeout 3 s → `unknown`; 5xx/conn refused → `retryable`; 4xx → `permanent`);
   - ui = delivered immediately.
   - **Adding Twilio must be one new file.** Keep provider selection in a dict.
4. **Delivery worker** `python -m alerting.delivery.worker [--channels sms,phone]`:
   - Claim due rows (`state in (pending, retrying, unknown) and next_attempt_at <= now()`, `ORDER BY priority, next_attempt_at`, `FOR UPDATE SKIP LOCKED`, set `state = sending`, `locked_until = now() + 30s`) and commit.
   - Send **outside** the transaction, then record a `delivery_attempts` row plus the state transition.
   - Backoff `min(60, 2^n) ± 20 %`, honour `retry_after`. Max 8 attempts / 15 min → `failed`.
   - Per-channel circuit breaker (5 consecutive failures → open 10 s). Then `NOTIFY notifications`.
   - Expired leases are re-claimable, so a crashed worker loses nothing.
   - `GET /v1/alerts/{id}/notifications`, `GET /v1/notifications?state=`.

### By G2 (Sat 04:00)
5. **Runbooks** `runbooks/store.py`:
   - Load `files.runbooks()`; detect the format; match by `rules[]`, `matches` (RULES' `dsl.compile` accepts the flat map form), or the yaml-steps' own `matches`. Choose one runbook per alert in `plan()`.
   - `GET /v1/alerts/{id}/runbook` → rendered steps with params filled from the representative event (`templates.render`):
     - **yaml-steps:** check / action / if;
     - **markdown:** numbered or checkbox lines → manual steps; detect "approval" wording → `approval: required`;
     - **text:** one manual step + `can_structure: true`;
     - **url:** link.
   - `GET/POST /v1/runbooks` (any format, origin `ui`).
6. **Actions** `runbooks/actions/` (one file each, `REGISTRY` dict): `http_get` (allow-listed hosts only: `counterparty-service`, `mock-ops`), `retry_workflow`, `restart_service`, `call_webhook` (allow-list), `open_ticket`, `escalate` (creates an `escalation` notification to the target team's on-call via `plan()`). Unknown action name → never executed; shown as manual.
   - **mock-ops** (`mocks/mock_ops/`, port 8200, compose network alias `counterparty-service`): `GET /health` (toggle), `POST /workflows/{id}/retry` (configurable: fail N times, then succeed), `POST /services/{name}/restart`, `POST /tickets`.
7. **Execute + approve + audit:** `POST /v1/alerts/{id}/actions {step_index}` with `X-User` → `action_runs` row:
   - `approval: none` → run now;
   - `approval: required` → `pending_approval` + 4-digit `approval_code` (10-minute expiry) + an `approval_request` notification (SMS) to the approvers.
   - `POST /v1/action-runs/{id}/approve|reject` (403 unless the actor is in the team's `approvers`).
   - Results go to audit (`action.execute`, `action.approve`), then `NOTIFY actions`.
   - **Auto-close:** an action result with `resolves_alert` or a passing health check → resolve the alert (`resolved_by = system`, reason).
   - Track the failure count for `if: "retry fails twice"` and show "escalation suggested" after 2 failed retries.
8. `GET/PUT /v1/teams`, `GET/PUT /v1/users/{id}/preferences` (origin `ui` version; the next plan uses it).

### By G3 (Sat 07:30), P0.5 then P1
9. **P0.5 runbook structuring** `assist/runbook_structure.py` + `POST /v1/runbooks/structure {text, sample_alert_id?}`:
   - `llm.structured(StructuredRunbook, …)` with the action REGISTRY (names + params schemas + default approval) and the alert's available fields.
   - Output steps: `{kind: check|action|manual|wait|escalate, text, action?, params?, approval}`.
   - **Validator:** unknown action → manual; any `restart|failover|rollback|escalate|delete` → `approval: required`; params referencing missing fields → flagged.
   - Heuristic fallback: split sentences and numbered lines into manual steps, plus keyword → action suggestions.
   - Example: "Restart the pricing-engine instance named in the alert" → `restart_service(name={{instance}})`, approval required.
10. **Reply-to-approve:** `POST /v1/inbound/{channel} {from, body}`: parse `YES|NO <code>`, map the sender's phone/email to a user, check approver + code + expiry + single use → approve or reject. fake-telecom's reply box calls this.
11. **Channel fallback:** when the breaker for a channel stays open > 30 s for a `critical` notification, create a `fallback` notification on the next preferred channel (`fallback_of` set) and keep retrying the original.
12. P1-late: `POST /v1/runbooks/{id}/propose-update {alert_id}`: an LLM drafts a runbook diff from the audit trail of a resolved alert (task 2.6 optional). The owner accepts it as a new version.

## Acceptance (Yi checks with the golden runner)
`settlements-positive` (notifications), `rates-positive`, all `anna-*`, `settlements-runbook-rendered`, `approval-*`, `channel-down-no-loss` (0 lost), plus `kill -9` of a delivery worker mid-send → the lease expires → the row is re-sent with the **same** `Idempotency-Key` (fake-telecom shows 1 unique message).

## Pitfalls
- No HTTP calls inside a DB transaction. Claim, commit, send, record.
- A timeout is not a failure: mark it `unknown` and retry with the same key.
- Times: Anna's window is in **her** timezone, not the server's. Use the injected clock (`clock_for_event`).

## Starter prompt for your coding agent
> You are implementing the DELIVERY role of the Alert Funnel hackathon project. Read `docs/BRIEF.md`, `docs/CONTRACT.md` and `docs/handoff/DELIVERY.md` fully first. Work only inside the directories listed under "You own" in DELIVERY.md. Do not edit shared files (`contracts.py`, `llm.py`, `db.py`, `clock.py`, `templates.py`, `api/app.py`, `001_init.sql`); if one needs a change, stop and tell me. Start with tasks 1–2 (directory + pure `routing.plan`) test-first, using `config/teams.yaml`, `config/rules.yaml` and the Anna/Settlements/Rates cases in `fixtures/golden/expectations.yaml`, then providers and the delivery worker. Commit after each task and run `make test`. Python 3.12, psycopg 3 async, httpx. Report what you ran and what you did not run.
