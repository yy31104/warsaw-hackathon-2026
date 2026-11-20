"""Confine all runbook HTTP traffic to the deployment-configured mock-ops endpoint."""

import os
from urllib.parse import quote, urlsplit

import httpx

from alerting.contracts import ActionResult


def local_path(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"counterparty-service", "mock-ops"}
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 8200)
    ):
        raise ValueError("URL is outside the runbook service allow-list")
    return (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


def segment(params, key):
    value = str(params.get(key) or "")
    if not value or value in (".", "..") or "/" in value:
        raise ValueError(f"invalid {key}")
    return quote(value, safe="")


async def request(method, path, ctx, body=None):
    url = os.getenv("MOCK_OPS_URL", "http://localhost:8200").rstrip("/") + path
    async with httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client:
        response = await client.request(
            method, url, json=body, headers={"Idempotency-Key": f"action-{ctx.run_id}"}
        )
    if not response.is_success:
        return ActionResult(ok=False, error=f"mock-ops HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text[:2048]}
    if not isinstance(data, dict):
        data = {"value": data}
    ok = data.get("ok", data.get("success", True)) is not False
    return ActionResult(
        ok=ok, output=data, resolves_alert=ok and data.get("resolves_alert") is True
    )
