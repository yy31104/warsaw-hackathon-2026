# Mock API responses (for UI work before the backend lands)

Illustrative shapes only, so the UI can be built in parallel. **The numbers are fake** (not measurements). Backend owners: if your real response differs, update the file here in the same PR so the UI stays in sync.

| File | Endpoint | Owner of real endpoint |
|---|---|---|
| `alerts.json` | `GET /v1/alerts?team=&status=` | BACKEND |
| `alert_detail.json` | `GET /v1/alerts/{id}` | BACKEND |
| `notifications.json` | `GET /v1/alerts/{id}/notifications` | BACKEND |
| `runbook.json` | `GET /v1/alerts/{id}/runbook` | BACKEND |
| `rules.json` | `GET /v1/rules` | DATA |
| `preview.json` | `POST /v1/rules/{id}/preview` | DATA |
| `schema_issues.json` | `GET /v1/schema-issues` | DATA |
| `schema_suggest.json` | `POST /v1/schemas/suggest` | DATA |
| `funnel.json` | `GET /v1/stats/funnel` | DATA |
| `stream.ndjson` | `GET /v1/stream` (SSE): one `{type, data}` per line, replay with ~300 ms spacing | DATA |
