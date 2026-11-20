"""Shared LLM helper. SHARED FILE — see docs/CONTRACT.md §11.

Rules:
  * Never call this on the alert path (ingest -> rules -> routing -> delivery).
  * Callers MUST handle `None` (timeout / API error / refusal / no key) with a deterministic fallback.
  * Redact before sending. `redact()` is applied to the user text here as a last line of defence.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TypeVar

from pydantic import BaseModel

log = logging.getLogger(__name__)

MODEL = os.getenv("ASSIST_MODEL", "claude-opus-5-5")
MAX_INPUT_CHARS = 8_000

T = TypeVar("T", bound=BaseModel)

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "<phone-or-number>"),
    (re.compile(r"\b\d{13,19}\b"), "<card-like>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1<token>"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)\S+"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    """Redact free TEXT (runbook text, rule descriptions). Never run it on a raw JSON string:
    it can mask numbers (e.g. epoch-ms timestamps) and break the JSON. For events use describe_sample()."""
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text[:MAX_INPUT_CHARS]


_SENSITIVE_PATH = re.compile(r"(?i)(card|pan|iban|account|acct|phone|msisdn|email|password|secret|token|api[_-]?key)")


def describe_sample(fields: dict, max_fields: int = 200) -> list[dict]:
    """Structured, redacted view of ONE event for the LLM: [{path, type, example}].

    `fields` is the flattened event (path -> value). Strings are redacted and truncated; numbers and booleans keep
    their value (needed to recognise epoch-ms times, amounts, codes) unless the path itself looks sensitive.
    Serialise the result with json.dumps(...) and send that — it is always valid JSON.
    """
    out: list[dict] = []
    for path in sorted(fields)[:max_fields]:
        value = fields[path]
        kind = "null" if value is None else type(value).__name__
        if isinstance(value, (dict, list)):
            example = f"<{kind} len={len(value)}>"
        elif _SENSITIVE_PATH.search(path):
            example = "<redacted>"
        elif isinstance(value, str):
            example = redact(value)[:120]
        else:
            example = value
        out.append({"path": path, "type": kind, "example": example})
    return out


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic  # imported lazily so the core never depends on it

        _client = anthropic.AsyncAnthropic(max_retries=1)
    return _client


def enabled() -> bool:
    return os.getenv("ASSIST_ENABLED", "1") == "1" and bool(
        os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    )


async def structured(output_model: type[T], *, system: str, user: str, timeout_s: float = 20.0) -> T | None:
    """One structured-output call. Returns a validated `output_model` or None."""
    if not enabled():
        return None
    try:
        client = _get_client()
        response = await asyncio.wait_for(
            client.messages.parse(
                model=MODEL,
                max_tokens=8_000,
                system=system,
                messages=[{"role": "user", "content": redact(user)}],
                output_format=output_model,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning("llm timeout after %.1fs (%s)", timeout_s, output_model.__name__)
        return None
    except Exception as exc:  # API/network errors: fall back, never break the caller
        log.warning("llm call failed (%s): %s", output_model.__name__, exc)
        return None
    if getattr(response, "stop_reason", None) == "refusal":
        log.warning("llm refused (%s)", output_model.__name__)
        return None
    return response.parsed_output
