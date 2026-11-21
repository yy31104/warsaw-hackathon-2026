"""Independent acceptance inputs for approval decisions when the model is unavailable."""

import socket
from itertools import groupby
from operator import itemgetter

import httpx2
import openai
import pytest

from alerting.assist.runbook_structure import structure

# Each row is (section heading, operator instruction, required approval).
# Both books mix read-only and mutating instructions across multiple sections.
BOOKS = (
    (
        ("Machine room", "Reboot service slate-printer.", True),
        ("Machine room", "Inspect the recorded time of the last service reboot.", False),
        ("Machine room", "Bounce the affected service.", True),
        ("Escalation desk", "Page team north-auditors.", True),
        ("Escalation desk", "Record the duty shift and escalate to team hill-operators.", True),
        ("Callbacks", "Invoke the webhook http://mock-ops/test/press.", True),
        ("Callbacks", "Call the designated webhook.", True),
    ),
    (
        ("Machine room", "If the morning check still fails, restart service maple-relay.", True),
        ("Machine room", "Write down the sequence number and reboot service amber-loop.", True),
        ("Escalation desk", "Escalate to whoever owns this queue.", True),
        ("Escalation desk", "If the spill counter rises again, page the on-call operator.", True),
        ("Callbacks", "Read the webhook delivery history on the dashboard.", False),
        ("Callbacks", "When the gauge reaches the red zone, post to the webhook http://mock-ops/test/gauge.", True),
        ("Callbacks", "Record the gauge position and send a POST to the callback at http://mock-ops/test/gauge-mark.", True),
    ),
)


def operator_text(book):
    lines = []
    for heading, group in groupby(book, key=itemgetter(0)):
        lines.append(f"## {heading}")
        lines.extend(f"- {text}" for _, text, _ in group)
    return "\n".join(lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("no-key", "api-error", "network-disabled"))
async def test_approval_acceptance(failure, monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "1")
    monkeypatch.setenv("ASSIST_PROVIDER", "openai")
    monkeypatch.setenv("ASSIST_MODEL", "gpt-6-luna")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    if failure == "no-key":
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-approval-acceptance-only")

    # Disable real DNS and socket connections in every mode. For the network
    # outage, let the SDK see its ordinary connection exception. For the API
    # outage, let it parse a genuine HTTP status response through its own code.
    def forbid_socket(*args, **kwargs):
        pytest.fail("acceptance check attempted external socket access")

    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, forbid_socket)
    monkeypatch.setattr(socket, "getaddrinfo", forbid_socket)
    attempted = []

    async def transport(request):
        attempted.append(request.url.path)
        if failure == "no-key":
            pytest.fail("no-key mode attempted a provider request")
        if failure == "network-disabled":
            raise httpx2.ConnectError("network unavailable", request=request)
        return httpx2.Response(503, request=request, json={"error": {
            "message": "Simulated service outage", "type": "server_error", "code": "unavailable"}})

    http_client = openai.DefaultAsyncHttpxClient
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kwargs: http_client(
        transport=httpx2.MockTransport(transport), **kwargs))
    required = unguarded = safe = unnecessary = safe_missing = 0
    for book in BOOKS:
        result = await structure(operator_text(book))
        assert result["source"] == "heuristic"
        steps = result["draft"]["steps"]
        for _, text, risky in book:
            approvals = [s.get("approval") for s in steps if s.get("text") == text]
            if risky:
                required += 1
                # An omitted instruction, missing approval, or unguarded duplicate
                # cannot count as a protected operator instruction.
                unguarded += int(not approvals or any(a != "required" for a in approvals))
            else:
                safe += 1
                safe_missing += int(not approvals)
                unnecessary += int("required" in approvals)
    assert (required, safe) == (12, 2)
    if failure == "no-key":
        assert not attempted
    else:
        assert attempted and all(p.endswith("/responses") for p in attempted)
    print(f"{failure} required_unguarded={unguarded}/{required} "
          f"safe_unnecessary_approvals={unnecessary}/{safe} safe_missing={safe_missing}/{safe}")
    assert unguarded == 0, f"{unguarded}/{required} risky instructions lack required approval"
