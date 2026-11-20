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

## Reproduce the ingest tests

Run `make test` from the repository root. Database tests use `DATABASE_URL` and create their own
temporary schemas. Set `INGEST_TEST_RABBITMQ_URL` to a test broker (for local Compose,
`amqp://guest:guest@localhost:5672/`) to include the 1,000-message test and the worker crash
between commit and acknowledgement. The test uses a dedicated queue and removes it afterwards.
