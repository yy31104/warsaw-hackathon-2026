# Phone simulator

Run `docker compose up -d --build fake-telecom` from the repository root and open
[the phone inbox](http://localhost:8100/). Select a phone number to see its latest 50
SMS and call receipts. The simulator stores receipts and the current delivery mode
in its own SQLite volume so they survive a restart. It timestamps each arrival at the receiver.

For native development, run `uv run --project backend uvicorn mocks.fake_telecom.app:app --port 8100`
from the repository root. Set `FAKE_TELECOM_DB` to choose the SQLite file. The native default
is `mocks/fake_telecom/.data/receipts.sqlite3`.

## Send and inspect

`POST /sms` and `POST /call` accept `{"to":"+12025550100","text":"Settlement TIMEOUT"}`.
Send the notification ID in `Idempotency-Key`. The optional `X-Gen-Sent-At` header
is the generator timestamp in Unix milliseconds. A successful response returns the
same key in `X-Message-Id`. Repeating the same delivery keeps its first receipt and timestamps.
Reusing a key for another channel, number or text returns HTTP 409.

`GET /messages` returns `{"items":[...]}` in newest-first order. Each receipt has
`idempotency_key`, `channel` (`sms` or `phone`), `to`, `text`, `gen_sent_at` and
`received_at`. Both timestamps use Unix milliseconds. A missing sender timestamp is
`null`. Filter with `?to=%2B12025550100` or add `limit`. `GET /numbers` lists phones
that have received a notification.

## Simulate a fault

Use the delivery mode selector or send `{"mode":"down"}` to `POST /mode`.
`GET /mode` returns the current setting. Select `ok` to resume normal delivery.

| Mode | What the sender sees | What the phone stores |
| --- | --- | --- |
| `ok` | HTTP 200 and the message ID | One receipt |
| `down` | HTTP 503 while the gateway is unavailable | No new receipt |
| `503` | HTTP 503 with `Retry-After: 2` | No new receipt |
| `slow` | Success after four seconds, beyond Yi's three-second timeout | Receipt saved immediately |
| `drop_response` | The sender times out waiting for the acknowledgement | Receipt saved immediately |

## Reply from a phone

Set the same `INBOUND_SECRET` on this service and the alert API. Compose reads it from
the project `.env`. Set `ALERTING_API_URL` to the base URL accessible from the simulator.
Compose defaults to the host API on port 8000, and the native default is `http://127.0.0.1:8000`.

Select the phone that received the approval request and type `YES` or `NO` followed by
the exact code printed in that SMS, including any leading zeros. Press **Send**.
The page posts `{"from":"<selected number>","body":"YES <code>"}` to
`/reply`. The server forwards it to `/v1/inbound/sms` with `X-Provider-Secret`.
The page reports success when the alert API accepts the reply and shows its reason
when the reply is rejected.
