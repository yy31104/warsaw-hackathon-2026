# Mock API responses (for UI work before the backend lands)

Illustrative shapes only, so the UI can be built in parallel. **The numbers are fake** (not measurements). Backend owners: if your real response differs, update the file here in the same PR so the UI stays in sync.

| File | Endpoint | Owner of real endpoint |
|---|---|---|
| `alerts.json` | `GET /v1/alerts?team=&status=` | DEV3 |
| `alert_detail.json` | `GET /v1/alerts/{id}` | DEV3 |
| `notifications.json` | `GET /v1/alerts/{id}/notifications` | DEV4 |
| `runbook.json` | `GET /v1/alerts/{id}/runbook` | DEV3 (render by DEV4) |
| `rules.json` | `GET /v1/rules` | DEV2 |
| `preview.json` | `POST /v1/rules/{id}/preview` | DEV2 |
| `schema_issues.json` | `GET /v1/schema-issues` | DEV2 |
| `schema_suggest.json` | `POST /v1/schemas/suggest` | DEV2 |
| `funnel.json` | `GET /v1/stats/funnel` | DEV1 |
| `stream.ndjson` | `GET /v1/stream` (SSE): one `{type, data}` per line, replay with ~300 ms spacing | DEV4 |
