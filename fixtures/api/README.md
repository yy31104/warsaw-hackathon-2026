# Mock API responses (for UI work before the backend lands)

Illustrative shapes only, so the UI can be built in parallel. **The numbers are fake** (not measurements). Backend owners: if your real response differs, update the file here in the same PR so the UI stays in sync.

| File | Endpoint | Owner of real endpoint |
|---|---|---|
| `alerts.json` | `GET /v1/alerts?team=&status=` | DEV1 Yi |
| `alert_detail.json` | `GET /v1/alerts/{id}` | DEV1 Yi |
| `notifications.json` | `GET /v1/alerts/{id}/notifications` | DEV1 Yi |
| `runbook.json` | `GET /v1/alerts/{id}/runbook` | DEV1 Yi |
| `rules.json` | `GET /v1/rules` | DEV2 daizhongtian |
| `preview.json` | `POST /v1/rules/{id}/preview` | DEV2 daizhongtian |
| `schema_issues.json` | `GET /v1/schema-issues` | DEV2 daizhongtian |
| `schema_suggest.json` | `POST /v1/schemas/suggest` | DEV2 daizhongtian |
| `funnel.json` | `GET /v1/stats/funnel` | DEV2 daizhongtian |
| `stream.ndjson` | `GET /v1/stream` (SSE): one `{type, data}` per line, replay with ~300 ms spacing | DEV2 daizhongtian |
