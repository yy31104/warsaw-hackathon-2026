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


def storage_key(key: str | None) -> str | None:
    # PostgreSQL btree entries have a size limit. Opaque long IDs must still be
    # usable and must not collide with a producer-supplied spelling of our prefix.
    if not key or not key.strip():
        return None
    encoded = key.encode("utf-8", errors="surrogatepass")
    if len(encoded) > 1024 or key.startswith("sha256-id:") or re.search(r"[\x00\ud800-\udfff]", key):
        return "sha256-id:" + hashlib.sha256(encoded).hexdigest()
    return key


def identity(message_id: str | None, schema: dict, fields: dict, source: dict, payload_hash: str) -> str | None:
    if message_id and message_id.strip():
        return storage_key(message_id)
    template = schema.get("event_id")
    if template and all(lookup(fields, path) not in (None, "") for path in variables(template)):
        return storage_key(render(template, fields))
    return payload_hash if source.get("dedup") == "payload_hash" else None


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
_PRIVATE_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|email(?:[_-]?address)?|"
    r"phone(?:[_-]?number)?|msisdn|iban|(?:^|[_-])pan|card(?:[_-]?(?:number|pan))?|"
    r"account[_-]?number)$", re.I,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CREDENTIAL = re.compile(r"(?i)(\bbearer\s+|\b(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+")
_CONTACT_NUMBER = re.compile(r"(?<![\w-])\+\d(?:[ ()-]?\d){7,14}(?![\w-])")


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


def _labels(fields: dict, schema: dict) -> dict:
    paths = schema.get("aliases", {})
    labels = {"$schema": render(schema["name"], fields) if schema else None}
    for key in ("service", "environment", "error_code", "message"):
        value = lookup(fields, paths[key]) if key in paths else None
        if key in ("service", "environment"):
            value = str(value) if isinstance(value, (str, int, float)) else None
        labels["$" + key] = value
    return labels


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
    return _project(original, _redacted(raw), source, schemas, digest, received_at, message_id, trace)


def reclassify(event: CanonicalEvent, schemas: Sequence[dict]) -> CanonicalEvent:
    """Project the retained sample in place, keeping its original transport identity and hash."""
    source = {"id": event.source_id, "transport": event.transport}
    heuristic = event.trace.get("identity_method") == "payload_hash" and event.source_event_id == event.payload_hash
    if heuristic:
        source["dedup"] = "payload_hash"
    projected = _project(flatten(event.raw), event.raw, source, schemas, event.payload_hash,
                         event.received_at, None, event.trace)
    projected.id = event.id
    if event.source_event_id is not None and not heuristic:
        projected.source_event_id = event.source_event_id  # already in storage form
    projected.trace = event.trace
    return projected


def _project(original, public, source, schemas, digest, received_at, message_id, trace):
    metadata = {"$source": source["id"], "$transport": source["transport"]}
    original.update(metadata)
    schema = detect_schema(original, schemas) or {}
    aliases = schema.get("aliases", {})
    severity = _severity(original.get(aliases.get("severity")), schema)
    occurred_at = _time(original.get(aliases.get("time")), schema.get("time_format", "iso"))
    metadata.update({"$severity": severity.value, "$time": occurred_at.isoformat() if occurred_at else None})
    original.update(metadata)
    original.update(_labels(original, schema))
    source_event_id = identity(message_id, schema, original, source, digest)
    trace = dict(trace or {})
    if not schema and not (message_id and message_id.strip()) and source.get("dedup") == "payload_hash":
        trace["identity_method"] = "payload_hash"

    fields = flatten(public)
    fields.update(metadata)
    fields.update(_labels(fields, schema))
    template = schema.get("event_id")
    if template and source_event_id and not (message_id and message_id.strip()):
        private_key = render(template, original)
        if source_event_id == storage_key(private_key) and private_key != render(template, fields):
            # A configured ID may reference masked data. Keep the key usable for
            # retransmissions without revealing its underlying value via GET /events.
            source_event_id = "sha256-id:" + hashlib.sha256(private_key.encode()).hexdigest()
    return CanonicalEvent(
        source_id=source["id"], transport=source["transport"], payload_hash=digest,
        source_event_id=source_event_id, schema_id=schema.get("id"),
        schema_version=schema.get("version", 1) if schema else None, schema_name=fields["$schema"],
        severity=severity, service=fields["$service"], environment=fields["$environment"],
        occurred_at=occurred_at, received_at=received_at, raw=public, fields=fields,
        lane="critical" if severity == Severity.critical else "normal", status="pending" if schema else "needs_review",
        trace={**trace, "received_at": received_at.isoformat()},
    )
