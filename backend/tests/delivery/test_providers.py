import json

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


@pytest.mark.parametrize("channel", ["sms", "phone"])
async def test_merged_telecom_sends_all_member_links_in_one_request(channel):
    members = [{"title": f"rule {i}", "link": f"http://localhost:5173/a/{i}"} for i in range(12)]
    message = MSG.model_copy(update={
        "short": "12 alerts", "body": "A previous email included a runbook.",
        "meta": {"merged_alerts": members},
    })
    requests = []

    def receive(request):
        requests.append((request.headers["Idempotency-Key"], json.loads(request.content)["text"]))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        result = await Telecom(channel, client).send(message, "+155501", "group-id")
    assert result.outcome == "delivered"
    expected = "\n".join(f"{member['title']} {member['link']}" for member in members)
    if channel == "phone":
        expected += " — press 1 to acknowledge"
    assert requests == [("group-id", expected)]


@pytest.mark.parametrize("count,stride,title_length", [(2, 1, 9000), (300, 1, 50), (1400, 43000001, 15)])
async def test_large_merged_notice_fits_gateway_limit(count, stride, title_length):
    members = [
        {"alert_id": aid, "title": f"member-{i} " + "ö" * title_length, "link": f"http://localhost:5173/a/{aid}"}
        for i, aid in enumerate(range(1, count * stride + 1, stride))
    ]
    received = []

    def gateway(request):
        text = json.loads(request.content)["text"]
        received.append(text)
        return httpx.Response(200 if 1 <= len(text) <= 4096 else 422)

    msg = MSG.model_copy(update={"meta": {"merged_alerts": members, "event_id": 77}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        result = await Telecom("phone", client).send(msg, "+155501", "group-id")
    assert result.outcome == "delivered"
    (text,) = received
    assert text.endswith(" — press 1 to acknowledge")
    if count == 2:
        assert all(m["link"] in text and m["title"][:12] in text for m in members)
    else:
        assert f"{count} alerts" in text and "http://localhost:8000/v1/events/77/explain" in text
        if stride == 1:
            assert "1-300" in text and "http://localhost:5173/a/" in text
