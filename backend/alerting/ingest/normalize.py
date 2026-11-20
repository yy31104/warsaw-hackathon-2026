"""Turn one input into a CanonicalEvent. The caller supplies config and the receive time."""
import hashlib
import json
import re
import shlex
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from alerting.contracts import CanonicalEvent, Severity
from alerting.templates import lookup, render, variables

MAX_BYTES = 256 * 1024
MAX_DEPTH = 20
MAX_PATHS = 2000


class InvalidEvent(ValueError):
    """A permanent input error that the transport can report to the producer."""


def decode_json(body: bytes) -> Any:
    if len(body) > MAX_BYTES:
        raise InvalidEvent("body exceeds 256 KiB")
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise InvalidEvent("invalid JSON") from exc


def parse_log_line(line: str, parser: dict) -> dict:
    if len(line.encode("utf-8")) > MAX_BYTES:
        raise InvalidEvent("log line exceeds 256 KiB")
    match = re.fullmatch(parser["pattern"], line.rstrip("\r\n"))
    if not match:
        raise InvalidEvent("log line does not match the configured parser")
    fields = match.groupdict()
    if group := parser.get("kv_group"):
        try:
            for pair in shlex.split(fields.pop(group) or ""):
                key, value = pair.split("=", 1)
                fields[key] = value
        except ValueError as exc:
            raise InvalidEvent("invalid logfmt key/value pair") from exc
    return fields


def flatten(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise InvalidEvent("event must be a JSON object")
    result = {}
    count = 0

    def visit(value, path, depth):
        nonlocal count
        if depth > MAX_DEPTH:
            raise InvalidEvent("event exceeds depth 20")
        count += 1
        if count > MAX_PATHS:
            raise InvalidEvent("event exceeds 2000 paths")
        if "\0" in path or (isinstance(value, str) and "\0" in value):
            raise InvalidEvent("NUL characters cannot be stored in JSONB")
        result[path] = value
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}", depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}.{index}", depth + 1)

    for key, value in raw.items():
        visit(value, key, 1)
    # A literal dotted key takes precedence over the same nested path, as in templates.lookup.
    result.update(raw)
    return result


def _matches(condition: dict, fields: dict) -> bool:
    # Bootstrap subset used by schema detection until the shared rule compiler is available.
    if "all" in condition:
        return all(_matches(child, fields) for child in condition["all"])
    if "any" in condition:
        return any(_matches(child, fields) for child in condition["any"])
    if "not" in condition:
        return not _matches(condition["not"], fields)
    field, op = condition["field"], condition["op"]
    if op == "exists":
        return field in fields
    if op == "eq":
        return field in fields and fields[field] == condition["value"]
    raise ValueError(f"unsupported schema detection operator: {op}")


def detect_schema(fields: dict, schemas: Sequence[dict]) -> dict | None:
    return next((schema for schema in schemas if _matches(schema["detect"], fields)), None)


def identity(message_id: str | None, schema: dict, fields: dict, source: dict, payload_hash: str) -> str | None:
    if message_id and message_id.strip():
        return message_id
    template = schema.get("event_id")
    if template and all(lookup(fields, path) not in (None, "") for path in variables(template)):
        return render(template, fields)
    if source.get("dedup") == "payload_hash":
        return payload_hash
    return None


def _severity(value, schema: dict) -> Severity:
    mapping = schema.get("severity_map", {})
    key = str(value)
    if key in mapping:
        return Severity(mapping[key])
    for label, severity in mapping.items():
        if str(label).casefold() == key.casefold():
            return Severity(severity)
    return Severity(schema.get("severity_default", "unknown"))


def _time(value, fmt: str) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if fmt in ("epoch_ms", "epoch_s"):
            return datetime.fromtimestamp(float(value) / (1000 if fmt == "epoch_ms" else 1), UTC)
        parsed = datetime.fromisoformat(str(value))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


# Storage redaction preserves JSON types, timestamps and long non-sensitive values.
# The LLM redactor truncates free text and is intentionally not used for stored events.
_PRIVATE_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|authorization|email|phone|msisdn|iban|card[_-]?(?:number|pan)|account[_-]?number)$", re.I)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CREDENTIAL = re.compile(r"(?i)(\bbearer\s+|\b(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+")
_CONTACT_NUMBER = re.compile(r"(?<![\w-])(?:\+\d(?:[ ()-]?\d){7,14}|\d{13,19})(?![\w-])")


def _redacted(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if _PRIVATE_KEY.search(key) else _redacted(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, str):
        if "@" in value:
            value = _EMAIL.sub("<email>", value)
        value = _CREDENTIAL.sub(r"\1<redacted>", value)
        return _CONTACT_NUMBER.sub("<redacted>", value)
    return value


def normalize(raw: dict, source: dict, schemas: Sequence[dict], *, received_at: datetime,
              message_id: str | None = None, trace: dict | None = None) -> CanonicalEvent:
    original = flatten(raw)
    try:
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidEvent("event contains an invalid JSON value") from exc
    if len(encoded) > MAX_BYTES:
        raise InvalidEvent("body exceeds 256 KiB")
    digest = hashlib.sha256(encoded).hexdigest()
    metadata = {"$source": source["id"], "$transport": source["transport"]}
    original.update(metadata)
    schema = detect_schema(original, schemas) or {}
    public = _redacted(raw)
    fields = flatten(public)
    fields.update(metadata)
    aliases = {name: lookup(fields, path) for name, path in schema.get("aliases", {}).items()}
    severity = _severity(aliases.get("severity"), schema)
    occurred_at = _time(aliases.get("time"), schema.get("time_format", "iso"))
    name = render(schema["name"], fields) if schema else None
    fields.update({f"${key}": aliases.get(key) for key in ("service", "environment", "error_code", "message")})
    fields.update({"$schema": name, "$severity": severity.value, "$time": occurred_at.isoformat() if occurred_at else None})
    return CanonicalEvent(
        source_id=source["id"], transport=source["transport"], payload_hash=digest,
        source_event_id=identity(message_id, schema, original, source, digest),
        schema_id=schema.get("id"), schema_version=schema.get("version", 1) if schema else None, schema_name=name,
        severity=severity, service=fields["$service"] if isinstance(fields["$service"], str) else None,
        environment=fields["$environment"] if isinstance(fields["$environment"], str) else None,
        occurred_at=occurred_at, received_at=received_at, raw=public, fields=fields,
        lane="critical" if severity == Severity.critical else "normal", status="pending" if schema else "needs_review",
        trace={**(trace or {}), "received_at": received_at.isoformat()},
    )
