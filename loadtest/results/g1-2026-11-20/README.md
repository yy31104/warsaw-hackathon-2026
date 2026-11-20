# G1 live acceptance

DEV2 ran this local integration on 20 November 2026 with PostgreSQL, RabbitMQ, the project's ingest, rule and delivery workers, the API, and the SQLite-backed phone simulator. Chrome ran the project frontend from `49e55ba` in API mode. The backend incorporated main `ec35445`. The raw results identify the tested publisher and record the notification IDs, database rows and per-tab observations. [Run data](g1.json)

| Check | Result | Observation |
| --- | --- | --- |
| One settlement timeout via RabbitMQ | PASS | Event 1 was processed by `settlements-wf-failed`. Sam's phone received one SMS and one call for alert 1. Two settlements tabs displayed that alert without refreshing. The pricing tab received zero matching events. |
| 500 further occurrences of the same failure | PASS | The database linked 501 distinct events to one alert. Both settlements tabs displayed counts of 1, 40, 302 and 501. Both also saw the subsequent critical escalation and resolution. |
| Provider outage and recovery | PASS | In both `down` and `503` modes the receiver stored zero messages during the failure. After recovery the delivery worker retried the SMS and call automatically and both were delivered. |
| Lost and slow acknowledgements | PASS | In `drop_response` and `slow` modes both receipts were already durable when the worker timed out. Retrying preserved the original receipt IDs and timestamps. In the lost-response case three SMS attempts produced one visible SMS. |
| SMS reply | PASS | Sam requested an action. Lee replied from the simulator using the code in the approval SMS. Yi's API recorded action 1 as rejected by Lee and both settlements tabs received the decision. |
| Publisher retransmission | PASS | Each CLI transport sent the same payload and message ID 1,000 times. PostgreSQL held one processed event and 999 duplicate audit records per source, with zero other rejections. |
| Rate, unique fields and stream keepalive | PASS | Both transports sent four distinct workflow IDs at a cap of four per second. Send intervals were 250–251 ms. A live stream received seven stats ticks before its first heartbeat at 15.015 s. |
| Backend regression | PASS | `make test` passed all 159 tests with PostgreSQL and RabbitMQ enabled. |

Screenshots record the phone receipts and both team views. [Sam's inbox](sam-sms-call.png) · [Settlements at 501](settlements-501.png) · [Pricing feed](pricing-feed.png)

## Repeat the main flow

Start the API, RabbitMQ consumer, normal and critical rule workers, delivery worker and fake telecom on a fresh test stack. The gateway and API must share `INBOUND_SECRET` for phone replies. Run the frontend with `VITE_MOCK=0` and `VITE_ALERTS_MODE=api`. Open two `/alerts` tabs filtered to `settlements` and one filtered to `pricing` before sending.

```sh
uv run --project backend python tools/publish.py --via amqp --source workflow-bus --file fixtures/events/workflow_failed.json --message-id g1-settlement
uv run --project backend python tools/publish.py --via amqp --source workflow-bus --file fixtures/events/workflow_failed.json --count 500 --unique workflowId
```

For the retransmission check, use an identical file and a fresh `--message-id` on both commands, with `--count 1000` on each. Select `--via amqp --source workflow-bus` and `--via http --source generic-webhook` respectively. Group the stored events and duplicate audit records by source and message ID.
