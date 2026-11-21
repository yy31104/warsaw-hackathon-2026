# Alert Funnel console

The console was planned on `origin/main` at `10c398e` and fast-forwarded to `d7a6884` for the published saturation evidence. The prototype supplies the layout, labels and interaction states. Production views read the APIs below. The tour alone shows a labelled example.

| Screen | Endpoint | Response fields used |
|---|---|---|
| Shell | `GET /v1/teams`, `GET /v1/assist/status`, `GET /v1/stream?team=` | Directory users and team membership for the existing simulated actor selector and team scope. `enabled`, `model` for assist. Alert, action, notification and configuration messages invalidate the matching queries. |
| Now | `GET /v1/alerts?team=&status=open`, `GET /v1/alerts?team=`, `GET /v1/alerts/{id}`, `POST /v1/alerts/{id}/ack` | `id`, `title`, `severity`, `team_id`, `status`, `occurrence_count`, `first_seen_at`, `next_escalation`. Sort the open hero by severity and oldest first. |
| Now | `GET /v1/stats/funnel?window=24h`, `GET /v1/stats/pending-approvals?team=`, `GET /v1/teams` | Funnel stages use `raw`, `accepted`, `alerts_open` (current open incidents) and `notifications.delivered`. Pending count uses `pending_approval_count`; incident detail provides `pending_actions`, including `run_id`, `requested_by` and `approvers`. Team `oncall.primary` drives the roster. |
| Incident | `GET /v1/alerts/{id}`, `GET /v1/events/{representative_event_id}/explain`, `GET /v1/alerts/{id}/notifications`, `POST /v1/alerts/{id}/ack`, `POST /v1/alerts/{id}/resolve` | `first_seen_at`, `acked_at/by`, `resolved_at/by`, `next_escalation.at/targets/channels`, `timeline`, `occurrences[].seen_at/event_id/fields`, `fields`, matching decision trace and notification `recipient/channel/state`. Explain uses the explicit representative event ID when present. |
| Incident runbook | `GET /v1/alerts/{id}/runbook`, `POST /v1/alerts/{id}/actions`, `POST /v1/action-runs/{run_id}/approve\|reject` | Rendered steps, step `index`, `approval`, `action`, `state`, `run_id`, `approved_by` and `approvers`. Use the persisted run ID from the matching pending action. Keep check-result and retry for unknown outcomes. |
| Sources | `GET /v1/schema-issues`, `GET /v1/events/{sample_event_id}`, `POST /v1/schemas/suggest` | Fingerprint, source, waiting count, sample fields and redacted raw event. Suggestion `draft`, `source`, optional `model` and `rationale`. Apply edits to `aliases`, `transforms`, `severity_map` and `time_format`. |
| Sources test and publish | `POST /v1/schemas/verify`, `POST /v1/schemas/preview?fingerprint=`, `POST /v1/schemas?fingerprint=&base_version=` | `all_pass`, per-sample `passed`, `actual`, `deciding_fields`, `unit_warnings` and optional structured problems. Use preview `matches` and `base_version`; show publish `reprocessed`. |
| Rules | `GET /v1/rules`, `POST /v1/rules/draft`, `POST /v1/rules/check`, `POST /v1/rules/{id}/preview`, `POST /v1/rules`, `PUT /v1/rules/{id}` | Rule `match`, `route`, `present`, optional `escalate`, `version`, `owner`, `origin`. Check `problems` and `violations`. Replay `before`, `after`, `diff` and sample count. Write using the current version and existing owner checks. |
| Proposals | `GET /v1/proposals`, `POST /v1/proposals`, `POST /v1/proposals/{id}/approve` | Existing proposal list, results and permission checks. Linked to Rules and from the incident. |
| Runbooks | `GET /v1/runbooks`, `POST /v1/runbooks/structure`, `POST /v1/runbooks` | Original content, format, version and owner. Structure `draft.content`, `source`, `model`, `warnings` and step problems when returned. Render publish errors and tie field or step paths to the editor. |
| On call | `GET /v1/teams`, `GET/PUT /v1/users/{id}/preferences` | Team on-call primary and secondary. Ordered `when`, `min_severity`, `channels` using the selected person's time zone. Sliders edit a bounded window. The existing editor handles all rows and channel changes. |
| Health | `GET /v1/stats/funnel?window=24h`, upstream `GET /health` and `GET /metrics` | `raw`, `processing_backlog`, `alerts_open`, `backlog`, `oldest_pending_s`, `at_risk`, service status and Prometheus counters. Benchmark reads the dated summaries in `loadtest/results/platform-bench`. |
| Intro | Session storage | Five fixed story beats, labelled Example. No network data. |

## Fields missing at the starting commit

- The response-time endpoint in the contract has no route on main. Now shows the benchmark p50 with its receiver sample size. Time to respond, time to fix and messages per incident await live metrics.
- The funnel exposes a 24-hour arrival and delivery window and a current open count. There is no 24-hour count of newly created incidents. The open count is labelled separately.
- Notifications expose confirmation state and attempt timings. They have no dedicated `delivered_at` property. The incident timeline provides a timestamp when it records a delivered notification.
- `representative_event_id` is stored in the database but excluded from alert detail on this commit. Why you is withheld until the detail includes that ID.
- M6 formats and the text parser suggestion endpoint are absent. The Sources flow shows the stored JSON. Schema drafts support `time_format` but have no timezone override. Preview samples can include an explicit offset.
- Directory lists on-call people but has no shift end or general backup wait time. A specific incident shows its scheduled wait using `next_escalation.at`.
- The canary runner has its own `/status`. The default Compose app exposes no canary age through the frontend's API. Health omits that tile.
- On the starting main there were no saturation results. The latest base includes `loadtest/results/saturation-ae69a28/batch.json`. Health charts sustained processing against offered rate for each measured worker count and key distribution.
- The replay response counts events losing all notifications and does not split that count by severity. The editor labels the total as such. Alert severity comes from the normalized event. The standard rule presentation has no severity override.
- This revision of runbook structuring returns warnings and drafts. The UI presents structured problems when provided and validates YAML shape and the required approval values before publishing.

## Delivery order

Now, Incident, Sources, Rules, Runbooks, On call, Health, Intro. Keep the existing API, session, permissions and stream, and retain legacy routes as fallbacks. Validate using the production frontend and a dedicated Compose project with the assist provider disabled. The reference HTML uses its own fake local state and is excluded from the build.
