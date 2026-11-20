# Delivery canary

The watchdog sends a unique synthetic event through the ordinary HTTP ingest every 60 seconds. The normal rule worker and SMS delivery worker handle it. The watchdog checks the fake gateway's own receipt for the probe ID, destination, channel and generation timestamp. End-to-end latency is the receiver's arrival time minus the sender's generation time, in milliseconds.

Apply migration 136 and merge the sections of [config.example.yaml](config.example.yaml) into the corresponding source, schema, rule and directory files. The schema uses the reserved source identity. Put it before general schema detectors. The local demo key must match on both ends. Run one instance pointing at the same API and fake gateway as the workers.

```sh
cd backend
CANARY_API_KEY=demo-canary-key uv run python -m alerting.canary
```

`ALERTING_API_URL` defaults to port 8000 on localhost and `FAKE_TELECOM_URL` to port 8100. `DELIVERY_TARGET_S` defaults to 30 seconds. Read `GET http://127.0.0.1:8300/status` for the latest probe, the last successfully measured latency, and success/miss counts since startup. Set `CANARY_HOST` and `CANARY_PORT` to change the listener.

Enable the watchdog once the shared rules engine applies the predicate in [policy.py](policy.py) to each event and rule, including recovery, before matching or taking group locks. W0/W1 own those integration points. The reserved rule opts in with `include_synthetic: true`. Migration 136 stamps `trace.synthetic=true` on the trusted canary source, including stored older pulses. This keeps ordinary inputs with a similarly named payload field on their usual route.

Each pulse updates the same synthetic incident. The reserved rule has no reminder timer. A miss logs an error and posts one SMS directly to the simulated ops destination, bypassing the notification database and delivery workers. If the fake gateway itself fails, the status shows an unconfirmed bypass and the error remains in the watchdog log. A final receiver read at the deadline has a 250 ms observation allowance. Receipts timestamped after the target always count as misses.

To exercise a delivery outage in an isolated stack, stop its SMS delivery worker and leave HTTP ingest, the rules worker and fake gateway running. At the next probe deadline the fake ops inbox should contain the bypass while the ordinary synthetic notification remains pending.
