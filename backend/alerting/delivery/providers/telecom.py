import os
from email.utils import parsedate_to_datetime
import httpx
from alerting.clock import SYSTEM
from alerting.contracts import Channel, SendResult


def retry_after(raw):
    if raw:
        try:
            return max(0, float(raw))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(raw) - SYSTEM.now()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return None


class Telecom:
    def __init__(self, channel, client):
        self.channel = Channel(channel)
        self.client = client
        path = "/sms" if self.channel == "sms" else "/call"
        self.url = os.getenv("FAKE_TELECOM_URL", "http://localhost:8100").rstrip("/") + path

    async def send(self, msg, contact, idempotency_key):
        headers = {"Idempotency-Key": idempotency_key}
        if msg.meta.get("gen_sent_at") is not None:
            headers["X-Gen-Sent-At"] = str(msg.meta["gen_sent_at"])
        try:
            res = await self.client.post(self.url, headers=headers, json={"to": contact, "text": msg.short}, timeout=3)
        except httpx.TimeoutException as exc:
            return SendResult(outcome="unknown", error=str(exc))
        except httpx.TransportError as exc:
            return SendResult(outcome="retryable", error=str(exc))
        if res.is_success:
            return SendResult(outcome="delivered", provider_ref=res.headers.get("X-Message-Id", idempotency_key))
        retryable = res.status_code >= 500 or res.status_code == 429
        return SendResult(outcome="retryable" if retryable else "permanent", error=f"HTTP {res.status_code}",
                          retry_after_s=retry_after(res.headers.get("Retry-After")))
