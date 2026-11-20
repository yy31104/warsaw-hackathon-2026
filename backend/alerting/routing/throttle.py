"""Count a recipient's matched deliveries in a fixed window across all alerts of the rule."""

import json
from datetime import timedelta

from alerting.clock import SYSTEM
from alerting.rules.engine import duration


def window(rule, clock):
    seconds = int(duration(rule["throttle"]["per"]).total_seconds())
    if seconds <= 0:
        raise ValueError("throttle window must be positive")
    stamp = clock.now().timestamp()
    bucket = int(stamp // seconds)
    return bucket, (bucket + 1) * seconds - stamp


def apply(intent, rule, recent_sends, clock):
    limit = rule["throttle"]["per_recipient"]
    count = recent_sends.get((intent.recipient, intent.channel.value), 0)
    if count < limit:
        return intent
    bucket, delay = window(rule, clock)
    suppressed = count - limit + 1
    bucket_id = json.dumps(
        [rule["id"], intent.recipient, intent.channel.value, bucket], separators=(",", ":")
    )
    message = f"{suppressed} more alerts from {rule['id']}"
    link = intent.rendered.link or ""
    text = f"{message[: 159 - len(link)]} {link}".strip()
    intent.kind = "throttle_summary"
    intent.dedup_key = (
        f"{intent.alert_id}:throttle_summary:{intent.recipient}:{intent.channel.value}:{bucket}"
    )
    intent.rendered = intent.rendered.model_copy(
        update=dict(
            title=message,
            body=message,
            short=text,
            meta={
                **intent.rendered.meta,
                "suppressed_count": suppressed,
                "throttle_bucket": bucket_id,
                "due_in": delay,
            },
        )
    )
    return intent


async def reserve(cur, rule, intents, clock):
    bucket, remaining = window(rule, clock)
    counts = {}
    for i in sorted(intents, key=lambda i: (i.recipient, i.channel.value)):
        await cur.execute(
            """insert into delivery_windows(rule_id,recipient,channel,bucket,hits,expires_at)
            values(%s,%s,%s,%s,1,%s) on conflict(rule_id,recipient,channel,bucket)
            do update set hits=delivery_windows.hits+1 returning hits-1 as previous""",
            (
                rule["id"],
                i.recipient,
                i.channel.value,
                bucket,
                SYSTEM.now() + timedelta(seconds=remaining),
            ),
        )
        counts[i.recipient, i.channel.value] = (await cur.fetchone())["previous"]
    return counts


async def lock_rules(cur, rules):
    for rule in sorted({r["id"] for r in rules if r.get("throttle")}):
        await cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,1))", (rule,))
