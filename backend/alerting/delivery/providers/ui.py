from alerting.contracts import Channel, SendResult


class UI:
    channel = Channel.ui

    async def send(self, msg, contact, idempotency_key):
        return SendResult(outcome="delivered", provider_ref=idempotency_key)
