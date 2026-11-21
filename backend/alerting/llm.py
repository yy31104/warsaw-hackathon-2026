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
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
MAX_INPUT_CHARS = 8_000
MAX_OUTPUT_TOKENS = 8_000

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


def selection() -> tuple[str | None, str | None]:
    provider = os.getenv("ASSIST_PROVIDER", "").strip().lower()
    return provider if provider in _KEYS else None, os.getenv("ASSIST_MODEL", "").strip() or None


def status() -> dict:
    provider, model = selection()
    active = bool(os.getenv("ASSIST_ENABLED", "1") == "1" and provider and model
                  and os.getenv(_KEYS[provider], "").strip())
    return {"enabled": active, "provider": provider, "model": model}


def enabled() -> bool:
    return status()["enabled"]


@dataclass(frozen=True)
class CallObservation:
    """Provider-reported token counts. Total input includes cache reads and writes."""

    provider: str
    model: str
    response_model: str | None
    outcome: str
    latency_ms: float
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None


_observer: ContextVar[tuple[Callable[[CallObservation], None] | None, int]] = ContextVar(
    "assist_observer", default=(None, MAX_OUTPUT_TOKENS)
)


@contextmanager
def observe_calls(callback: Callable[[CallObservation], None], *, max_output_tokens: int = MAX_OUTPUT_TOKENS):
    """Measure requests in this task, including billed responses that fail Pydantic validation."""
    if not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError(f"max_output_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
    token = _observer.set((callback, max_output_tokens))
    try:
        yield
    finally:
        _observer.reset(token)


def _observation(provider, model, metadata, outcome, elapsed_ms):
    def tokens(data, field):
        value = data.get(field) if isinstance(data, dict) else None
        return value if type(value) is int and value >= 0 else None

    usage = metadata.get("usage")
    total = tokens(usage, "input_tokens")
    if provider == "openai":
        details = usage.get("input_tokens_details") if isinstance(usage, dict) else None
        cached, written = tokens(details, "cached_tokens"), tokens(details, "cache_write_tokens")
    else:
        cached, written = tokens(usage, "cache_read_input_tokens"), tokens(usage, "cache_creation_input_tokens")
        if total is not None:
            total += (cached or 0) + (written or 0)
    return CallObservation(provider, model, metadata.get("model"), outcome, elapsed_ms,
                           total, cached, written, tokens(usage, "output_tokens"))


def _messages(user: str) -> list[dict]:
    safe_text = redact(user).encode("utf-8")[:MAX_INPUT_CHARS].decode("utf-8", errors="ignore")
    return [{"role": "user", "content": safe_text}]


async def count_openai_input(output_model: type[T], *, system: str, user: str, timeout_s: float = 8.0) -> int | None:
    """Use the free count endpoint to budget the exact structured input before an evaluation call."""
    config = status()
    if not config["enabled"] or config["provider"] != "openai":
        return None
    try:
        async with asyncio.timeout(timeout_s):
            import openai
            # Use the same strict schema conversion as the locked SDK's responses.parse.
            from openai.lib._parsing._responses import type_to_text_format_param

            async with openai.AsyncOpenAI(
                api_key=os.environ["OPENAI_API_KEY"].strip(), max_retries=0, timeout=timeout_s,
                http_client=openai.DefaultAsyncHttpxClient(),
            ) as client:
                response = await client.responses.input_tokens.count(
                    model=config["model"], instructions=system, input=_messages(user),
                    text={"format": type_to_text_format_param(output_model)},
                )
                count = response.input_tokens
                return count if type(count) is int and count >= 0 else None
    except Exception:
        log.warning("assist input token count unavailable (%s)", output_model.__name__)
        return None


async def structured(output_model: type[T], *, system: str, user: str, timeout_s: float = 8.0) -> T | None:
    """One redacted SDK request within the interactive deadline, or None for the local fallback."""
    config = status()
    if not config["enabled"]:
        return None
    provider, model = config["provider"], config["model"]
    observer, limit = _observer.get()
    loop = asyncio.get_running_loop()
    started = loop.time()
    metadata = {}

    async def capture(response):
        # SDK parsing can raise before returning the message. Read its usage first when measuring.
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            return
        if isinstance(body, dict):
            metadata.update(model=body.get("model"), usage=body.get("usage"), stop_reason=body.get("stop_reason"))

    if provider == "openai":
        import openai as sdk
        client_type = sdk.AsyncOpenAI
    else:
        import anthropic as sdk
        client_type = sdk.AsyncAnthropic

    outcome = "error"
    try:
        # Both the SDK's I/O timeout and the total wall-clock deadline cover this single attempt.
        async with asyncio.timeout_at(started + timeout_s):
            async with client_type(
                api_key=os.environ[_KEYS[provider]].strip(), max_retries=0, timeout=timeout_s,
                http_client=sdk.DefaultAsyncHttpxClient(event_hooks={"response": [capture]} if observer else {}),
            ) as client:
                if provider == "openai":
                    response = await client.responses.parse(
                        model=model, instructions=system, input=_messages(user), text_format=output_model,
                        max_output_tokens=limit, store=False, service_tier="default",
                    )
                    refused = any(part.type == "refusal" for item in response.output if item.type == "message"
                                  for part in item.content)
                    complete, parsed = response.status == "completed", response.output_parsed
                else:
                    response = await client.messages.parse(model=model, system=system, messages=_messages(user),
                                                           output_format=output_model, max_tokens=limit)
                    refused = response.stop_reason == "refusal"
                    complete, parsed = response.stop_reason == "end_turn", response.parsed_output
                if complete and not refused and isinstance(parsed, output_model):
                    outcome = "success"
                    return parsed
                outcome = "refusal" if refused else "invalid"
    except (TimeoutError, sdk.APITimeoutError):
        outcome = "timeout"
    except (ValidationError, ValueError, sdk.APIResponseValidationError):
        outcome = "refusal" if metadata.get("stop_reason") == "refusal" else "invalid"
    except Exception:
        # SDK/HTTP error messages can contain upstream response text and secrets.
        outcome = "error"
    finally:
        if observer:
            observer(_observation(provider, model, metadata, outcome, (loop.time() - started) * 1000))
    log.warning("assist %s (%s, %s)", outcome, provider, output_model.__name__)
    return None
