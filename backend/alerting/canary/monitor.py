"""Monitor the public ingress and the receiver independently of API/worker health."""
import asyncio
import logging
import math
import os
from dataclasses import dataclass, field
from uuid import uuid4

import httpx

from alerting.clock import SYSTEM

LOG = logging.getLogger(__name__)
CANARY_NUMBER = "+15550100990"
OPS_NUMBER = "+15550100991"


@dataclass(frozen=True)
class Settings:
    api_url: str
    telecom_url: str
    api_key: str = field(repr=False)
    target_s: float = 30

    def __post_init__(self):
        if not self.api_key or not math.isfinite(self.target_s) or self.target_s <= 0:
            raise ValueError("Set CANARY_API_KEY and a positive finite DELIVERY_TARGET_S")

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("ALERTING_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            os.getenv("FAKE_TELECOM_URL", "http://127.0.0.1:8100").rstrip("/"),
            os.environ["CANARY_API_KEY"],
            float(os.getenv("DELIVERY_TARGET_S", "30")),
        )


def observed_latency(messages, probe_id, sent_ms, target_s):
    for row in messages:
        if not isinstance(row, dict):
            continue
        if (row.get("to") != CANARY_NUMBER or row.get("channel") != "sms"
                or str(row.get("text", "")).split()[:2] != ["Canary", probe_id]
                or row.get("gen_sent_at") != sent_ms):
            continue
        received = row.get("received_at")
        if type(received) is int and 0 <= received - sent_ms <= target_s * 1000:
            return received - sent_ms
    return None


class Monitor:
    def __init__(self, client, settings, *, poll_s=.25):
        self.client = client
        self.settings = settings
        self.poll_s = poll_s
        self.last_probe = None
        self.last_e2e_latency_ms = None
        self.delivered = self.missed = 0
        self._latest_success = -1.0

    def status(self):
        return {"last_probe": self.last_probe, "last_e2e_latency_ms": self.last_e2e_latency_ms,
                "delivered": self.delivered, "missed": self.missed,
                "delivery_target_s": self.settings.target_s}

    async def probe(self):
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + self.settings.target_s
        probe_id = str(uuid4())
        sent = int(SYSTEM.now().timestamp() * 1000)
        result = {"id": probe_id, "sent_at_ms": sent, "outcome": "pending", "bypass": None}
        self.last_probe = result
        reason = "no matching receipt by deadline"
        try:
            async with asyncio.timeout_at(deadline):
                response = await self.client.post(
                    f"{self.settings.api_url}/v1/events/canary",
                    json={"probe_id": probe_id, "trace": {"synthetic": True}},
                    headers={"Idempotency-Key": probe_id, "X-Api-Key": self.settings.api_key,
                             "X-Gen-Sent-At": str(sent)},
                )
                if response.status_code != 202:
                    reason = f"ingest HTTP {response.status_code}"
        except (httpx.RequestError, TimeoutError) as exc:
            # Missing acknowledgement: the ingress may already have committed.
            reason = f"ingest {type(exc).__name__}"

        while True:
            final_check = loop.time() >= deadline
            # Confirm once at the cutoff: a receipt may arrive between polling ticks.
            # Only receiver timestamps within the original target qualify.
            read_deadline = loop.time() + .25 if final_check else deadline
            try:
                async with asyncio.timeout_at(read_deadline):
                    inbox = await self.client.get(
                        f"{self.settings.telecom_url}/messages",
                        params={"to": CANARY_NUMBER, "limit": 100},
                    )
                    inbox.raise_for_status()
                    latency = observed_latency(inbox.json()["items"], probe_id, sent, self.settings.target_s)
                if latency is not None:
                    self.delivered += 1
                    result.update(outcome="delivered", latency_ms=latency)
                    if start >= self._latest_success:
                        self.last_e2e_latency_ms, self._latest_success = latency, start
                    return
            except (httpx.HTTPError, TimeoutError, KeyError, TypeError, ValueError) as exc:
                reason = f"receiver {type(exc).__name__}"
            if final_check:
                break
            await asyncio.sleep(max(0, min(self.poll_s, deadline - loop.time())))

        self.missed += 1
        result.update(outcome="missed", reason=reason, bypass="unconfirmed")
        LOG.error("canary missed id=%s target_s=%s reason=%s", probe_id, self.settings.target_s, reason)
        try:
            async with asyncio.timeout(3):
                alarm = await self.client.post(
                    f"{self.settings.telecom_url}/sms", headers={"Idempotency-Key": f"canary-alarm-{probe_id}"},
                    json={"to": OPS_NUMBER, "text": f"Alert Funnel canary {probe_id} missed: {reason}"},
                )
                alarm.raise_for_status()
                result["bypass"] = "sent"
        except (httpx.HTTPError, TimeoutError) as exc:
            LOG.error("canary bypass unconfirmed id=%s error=%s", probe_id, type(exc).__name__)

    async def run(self, interval_s=60):
        # Fixed start-to-start cadence, including when the configured deadline exceeds one period.
        async with asyncio.TaskGroup() as tasks:
            while True:
                tasks.create_task(self.probe())
                await asyncio.sleep(interval_s)
