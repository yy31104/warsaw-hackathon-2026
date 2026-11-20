# tools (owner: DEV2 daizhongtian)

The planned publisher for demos and load tests is `publish.py`.

## Try the event inbox

Start the repository's PostgreSQL and RabbitMQ with `make infra`. Run `make api` in a separate terminal.
From the repository root, send this request twice with the same key. The first response reports one
accepted event. The second reports one duplicate and returns the same event ID.

```sh
curl -s http://localhost:8000/v1/events/alertmanager \
  -H 'Content-Type: application/json' -H 'X-Api-Key: demo-alertmanager-key' \
  -H 'Idempotency-Key: demo-42' --data-binary @fixtures/events/alertmanager_high_error_rate.json
```

Read the stored original and the normalized fields with `GET /v1/events/{id}`.
A webhook array uses one stable key per position when the request supplies an idempotency key.

From `backend/`, start the broker consumer with `uv run python -m alerting.ingest.amqp`. Publish
to a queue in `config/sources.yaml` and preserve the AMQP `message_id` when retrying.
This command imports the seven pricing log lines once.

```sh
uv run python -m alerting.ingest.logfile --source pricing-logs ../fixtures/events/pricing-engine.log
```

The sample log has no event ID. Each line counts as an occurrence, including lines read by another import.

## Review an unknown event

Apply `db/migrations/201_schema_review.sql` to an existing database. A fresh Compose database
applies it automatically. Start the API and open `/docs` to run the following calls.
On native Windows, run `uv run uvicorn alerting.api.app:app --loop asyncio:SelectorEventLoop --port 8000`
from `backend/` and open `http://127.0.0.1:8000/docs`.

1. Send `{"app":{"name":"payments-api"},"signal":"RED"}` to
   `POST /v1/events/generic-webhook` with `X-Api-Key: demo-generic-key`. Keep the event ID.
2. Open `GET /v1/schema-issues`. The sample is under fingerprint `app,signal`. Read its redacted
   original with `GET /v1/events/{id}`. Each accepted occurrence increments the issue count.
   The first occurrence creates a grey incident and a delivered UI notice for the fallback team.
3. Optionally call `POST /v1/schemas/suggest` with `{"fingerprint":"app,signal"}` to get an editable
   draft, or pass `{"sample":{...}}` to try another example. Names such as `app.name` suggest a
   service field. The reviewer supplies the meaning of vendor-specific levels such as `RED`.
   In `POST /v1/schemas/preview`, select the fingerprint and try the following draft.

```json
{
  "id": "vendor-signals",
  "name": "vendor.alarm",
  "detect": {"all": [{"field": "app.name", "op": "exists"}, {"field": "signal", "op": "exists"}]},
  "aliases": {"service": "app.name", "severity": "signal"},
  "severity_map": {"RED": "critical"}
}
```

4. Check `matches`, the field aliases and `missing_paths`. To confirm, send the same draft to
   `POST /v1/schemas` with the same fingerprint and the preview's `base_version`. Set
   `X-User: ops` for the demo's simulated reviewer identity. Membership comes from the configured
   fallback team. The response includes the saved version and the number of reprocessed events.
5. Read the original event ID again. It should now be `pending` with service `payments-api` and
   severity `critical`, ready for the rule worker. Another matching input uses the same mapping
   immediately. If some events only share the top-level keys, their issue stays open with a
   remaining sample. `GET /v1/schema-issues?status=resolved` shows completed reviews.

With the critical rule worker running, the configured `anna-payments-critical` rule picks up
the original event. In demo mode, setting `X-Demo-Clock: 2026-11-20T09:00:00+01:00` on the
original webhook puts an SMS for Anna in the notification queue. At 21:00 Warsaw time it queues email.

File-managed schemas retain their order and are read-only through the API. UI-managed mappings
are stored in PostgreSQL. Publication, replay and audit commit together. If a proposed identity
would merge distinct stored events, the API returns 409 and the reviewer can refine the identity.

Schema suggestions work locally with `ASSIST_ENABLED=0`. With a configured model, the helper
can fill missing aliases using bounded, redacted field descriptions. The response reports
`source` as `heuristic` or `llm`. A suggestion and its preview are both read-only.

## Reproduce the ingest tests

Run `make test` from the repository root. Database tests use `DATABASE_URL` and create their own
temporary schemas. Set `INGEST_TEST_RABBITMQ_URL` to a test broker (for local Compose,
`amqp://guest:guest@localhost:5672/`) to include the 1,000-message test and the worker crash
between commit and acknowledgement. The test uses a dedicated queue and removes it afterwards.
