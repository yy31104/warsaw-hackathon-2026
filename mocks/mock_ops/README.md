# mock-ops

This FastAPI service simulates the operations targeted by runbook actions and the data returned to investigators. Its JSON responses carry `"simulated": true`. Each service restart and ticket creates a synthetic ID.

## Run locally

From the repository root, install the locked backend dependencies and start the receiver on port 8200.

```sh
(cd backend && uv sync --frozen)
backend/.venv/bin/uvicorn --app-dir mocks/mock_ops service:app_factory --factory --port 8200
```

The backend defaults to `MOCK_OPS_URL=http://localhost:8200`. The receiver stores settings, retry counts and execution receipts in SQLite at `/tmp/alert-funnel-mock-ops.sqlite3`. Set `MOCK_OPS_DB` to a fresh file to start a separate demo.

## Run with Compose

```sh
docker compose -f docker-compose.yml -f mocks/mock_ops/compose.yaml up -d --build mock-ops
```

The override adds the `counterparty-service` alias on the default network, publishes port 8200 on localhost and keeps SQLite on the `mock_ops_state` volume. Set `MOCK_OPS_URL=http://mock-ops:8200` in the container running the backend API. The service and volume declarations for the main stack are in [compose.yaml](compose.yaml).

## Endpoints

| Request | Behavior |
| --- | --- |
| `POST /toggle` | An empty request flips health. Send `{"healthy":false}` or `{"healthy":true}` to set a specific state. Health starts true. |
| `POST /config` | Send `{"fail_n":2}` to set the number of failed retries per workflow. The default is zero. `POST /mode` also accepts explicit health and retry settings. |
| `GET /health` | HTTP 200 with the simulated counterparty's `healthy` flag. A healthy result sets `resolves_alert` for the backend health-check action. |
| `POST /workflows/{id}/retry` | Counts distinct attempts per workflow ID. Attempts 1 through `fail_n` return 503, then later attempts return 200. Changing `fail_n` preserves each workflow's attempt count. |
| `POST /services/{name}/restart` | Returns the service name and a synthetic `restart_id`. |
| `POST /tickets` | Accepts a JSON object and returns 201 with a synthetic `ticket_id` and the supplied title. |
| `GET /metrics/pool`, `GET /deployments` | Return synthetic pool occupancy and deployment revisions. Both accept a `service` query parameter. |
| `GET /executions/{key}` | Returns the stored `state` and backend-compatible action `result` for a recorded key. An unseen key returns 404. |
| `POST /demo/delay-next` | With `DEMO_MODE=1`, arm one delayed reply for `retry_workflow`, `restart_service` or `open_ticket`. Send the selected name in the JSON `action` field. |

Action and configuration POSTs require an `Idempotency-Key`. Repeating the same operation with that key returns the original HTTP status and body, including a simulated failure. Reusing it for a different operation or parameters returns 409. The receiver commits the simulated effect and receipt together. Runbook reads with a key capture a replayable snapshot. Reads without a key show current state.

To demonstrate **Check result**, arm the receiver before running the settlement runbook's **Retry workflow** step.

```sh
curl --fail-with-body -sS http://127.0.0.1:8200/demo/delay-next \
  -H 'Content-Type: application/json' -d '{"action":"retry_workflow"}'
```

The next fresh matching action commits its effect and receipt, then waits four seconds to send its reply. The backend reaches its three-second timeout and marks the run unknown. **Check result** retrieves the recorded outcome using the same action ID. Replays immediately return the stored response.

For a two-failure scenario, submit the following requests in order. Replaying a failed key leaves the counter at its original attempt. A fresh demo can use a new workflow ID and new keys.

```sh
curl -sS -X POST http://localhost:8200/toggle \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-health' -d '{"healthy":false}'
curl -sS -X POST http://localhost:8200/config \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-retry-settings' -d '{"fail_n":2}'
for attempt in 1 2 3; do
  curl -sS -w ' HTTP %{http_code}\n' -X POST http://localhost:8200/workflows/demo-1/retry \
    -H "Idempotency-Key: demo-retry-$attempt"
done
curl -sS http://localhost:8200/executions/demo-retry-1
```

## Test

```sh
cd backend
uv run pytest -q tests/tools/test_mock_ops.py
```

The suite uses a temporary SQLite database and the backend's real runbook HTTP adapter. It exercises lost replies, concurrent replays, failed retries and verification after a receiver restart.
