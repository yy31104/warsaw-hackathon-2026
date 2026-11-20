import json
import logging
from dataclasses import replace

import httpx
import pytest

from alerting.canary.monitor import CANARY_NUMBER, OPS_NUMBER, Monitor, Settings


def receiver(request, probe, latency=15):
    sent = probe["sent"]
    return {
        "channel": "sms", "to": CANARY_NUMBER, "text": f"Canary {probe['id']} http://ui/a/7",
        "gen_sent_at": sent, "received_at": sent + latency,
    }


async def exercise(handler, target=.2):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        settings = Settings("http://api", "http://telecom", "w3-test-key", target)
        monitor = Monitor(client, settings, poll_s=.005)
        await monitor.probe()
        return monitor.status()


def ingest(request, probe):
    assert request.url.path == "/v1/events/canary"
    assert request.headers["X-Api-Key"] == "w3-test-key"
    body = json.loads(request.content)
    assert body["trace"]["synthetic"] is True
    probe["id"] = body["probe_id"]
    assert request.headers["Idempotency-Key"] == probe["id"]
    probe["sent"] = int(request.headers["X-Gen-Sent-At"])
    return httpx.Response(202, json={"accepted": 1, "conflicts": 0, "rejected": []})


async def test_measures_receiver_time_and_ignores_other_messages():
    probe = {}
    reads = []

    def handler(request):
        if request.url.host == "api":
            return ingest(request, probe)
        assert request.url.path == "/messages" and request.url.params["to"] == CANARY_NUMBER
        reads.append(1)
        msg = receiver(request, probe)
        wrong = [dict(msg, text="Canary previous-probe"), dict(msg, to=OPS_NUMBER),
                 dict(msg, gen_sent_at=1), dict(msg, channel="phone")]
        return httpx.Response(200, json={"items": wrong + ([msg] if len(reads) == 2 else [])})

    status = await exercise(handler)
    assert status["last_probe"]["outcome"] == "delivered"
    assert status["last_e2e_latency_ms"] == 15
    assert len(reads) == 2


@pytest.mark.parametrize("unavailable", [False, True])
async def test_missed_deadline_calls_bypass_and_logs_even_if_gateway_down(caplog, unavailable):
    probe, alarms = {}, []

    def handler(request):
        if request.url.host == "api":
            return ingest(request, probe)
        if request.method == "GET":
            late = receiver(request, probe, latency=9999)
            return httpx.Response(200, json={"items": [late]})
        assert str(request.url) == "http://telecom/sms"
        assert json.loads(request.content)["to"] == OPS_NUMBER != CANARY_NUMBER
        assert request.headers["Idempotency-Key"] == f"canary-alarm-{probe['id']}"
        alarms.append(1)
        return httpx.Response(503 if unavailable else 200)

    with caplog.at_level(logging.ERROR):
        status = await exercise(handler, target=.02)
    assert status["last_probe"]["outcome"] == "missed"
    assert status["last_probe"]["bypass"] == ("unconfirmed" if unavailable else "sent")
    assert "canary missed" in caplog.text and alarms == [1]
    assert status["last_e2e_latency_ms"] is None


async def test_lost_ingest_ack_can_still_be_delivered():
    probe = {}

    def handler(request):
        if request.url.host == "api":
            ingest(request, probe)
            raise httpx.ReadTimeout("ack lost")
        assert request.url.path == "/messages"
        return httpx.Response(200, json={"items": [receiver(request, probe)]})

    assert (await exercise(handler))["last_probe"]["outcome"] == "delivered"


def test_settings_reject_invalid_deadlines():
    settings = Settings("http://api", "http://telecom", "test", 30)
    for target in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            replace(settings, target_s=target)


async def test_deadline_does_not_miss_delivery_between_poll_ticks():
    probe, reads = {}, []
    def handler(request):
        if request.url.host == "api":
            return ingest(request, probe)
        if request.url.path == "/sms":
            return httpx.Response(200)
        reads.append(1)
        return httpx.Response(200, json={"items": [] if len(reads) == 1 else [receiver(request, probe, 5)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monitor = Monitor(client, Settings("http://api", "http://telecom", "w3-test-key", .02), poll_s=1)
        await monitor.probe()
        assert monitor.status()["last_e2e_latency_ms"] == 5
