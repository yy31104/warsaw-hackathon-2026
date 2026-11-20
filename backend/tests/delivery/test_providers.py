import httpx
import pytest
from alerting.contracts import RenderedMessage
from alerting.delivery.providers.telecom import Telecom

MSG = RenderedMessage(title="incident", body="detail", short="short", meta={"gen_sent_at": 122})


@pytest.mark.parametrize(
    "status,outcome",
    [(200, "delivered"), (429, "retryable"), (503, "retryable"), (400, "permanent")],
)
async def test_telecom(status, outcome):
    def receive(request):
        assert request.url.path == "/call"
        assert request.headers["Idempotency-Key"] == "same-id"
        assert request.headers["X-Gen-Sent-At"] == "122"
        return httpx.Response(status, headers={"Retry-After": "5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        result = await Telecom("phone", client).send(MSG, "+155501", "same-id")
    assert result.outcome == outcome
    if status == 429:
        assert result.retry_after_s == 5


async def test_timeout_is_unknown():
    def timeout(request):
        raise httpx.ReadTimeout("ambiguous", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        assert (await Telecom("sms", client).send(MSG, "+155501", "key")).outcome == "unknown"
