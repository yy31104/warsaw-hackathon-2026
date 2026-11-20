"""Turn one input into a CanonicalEvent. The caller supplies config and the receive time."""
import hashlib
import json
import math
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, Underflow, localcontext
from sys import get_int_max_str_digits
from typing import Any

from alerting.contracts import CanonicalEvent, Severity
from alerting.rules.dsl import compile as compile_match, number
from alerting.templates import lookup, render, variables

MAX_BYTES = 256 * 1024
MAX_DEPTH = 20
MAX_PATHS = 2000
SchemaDetector = Callable[[dict], dict | None]


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


def compile_schemas(schemas: Sequence[dict]) -> SchemaDetector:
    """Compile a registry snapshot once for the entire ingest or replay batch."""
    checks = [(schema, compile_match(schema["detect"])) for schema in schemas]

    def select(fields):
        return next((schema for schema, check in checks if check(fields)[0]), None)

    return select


def detect_schema(fields: dict, schemas: Sequence[dict] | SchemaDetector) -> dict | None:
    select = schemas if callable(schemas) else compile_schemas(schemas)
    return select(fields)


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


def _set_labels(fields: dict, schema: dict, severity: Severity, occurred_at: datetime | None) -> None:
    fields["$severity"] = severity.value
    fields["$time"] = occurred_at.isoformat() if occurred_at else None
    paths = schema.get("aliases", {})
    labels = {"$schema": render(schema["name"], fields) if schema else None}
    for key in ("service", "environment", "error_code", "message"):
        value = lookup(fields, paths[key]) if key in paths else None
        if key in ("service", "environment"):
            value = str(value) if isinstance(value, (str, int, float)) else None
        labels["$" + key] = value
    fields.update(labels)


def transform_scales(transforms: dict) -> dict[str, Decimal]:
    if not isinstance(transforms, dict):
        raise InvalidEvent("transforms must be an object")
    scales = {}
    for path, transform in transforms.items():
        scale = number(transform.get("scale", 1)) if isinstance(transform, dict) else None
        if scale is None:
            raise InvalidEvent(f"invalid scale for {path}")
        scales[path] = scale
    return scales


def _apply_scales(fields: dict, scales: dict[str, Decimal]) -> None:
    originals, scaled_values = {}, {}
    for path, scale in scales.items():
        raw = lookup(fields, path)
        value = number(raw)
        if value is None:
            continue
        try:
            with localcontext() as context:
                context.prec = len(value.as_tuple().digits) + len(scale.as_tuple().digits)
                context.traps[Underflow] = True
                scaled = value * scale
            if scaled == scaled.to_integral_value():
                if scaled.adjusted() >= (get_int_max_str_digits() or MAX_BYTES):
                    raise InvalidEvent(f"scale for {path} exceeds the JSON integer limit")
                converted = int(scaled)
            else:
                converted = float(scaled)
                if not math.isfinite(converted) or converted == 0:
                    raise InvalidEvent(f"scale for {path} exceeds JSON number precision")
        except DecimalException as exc:
            raise InvalidEvent(f"scaled value for {path} exceeds JSON number precision") from exc
        originals[path + ".raw"], scaled_values[path] = raw, converted
    fields.update(originals)
    fields.update(scaled_values)


def _set_episode(fields: dict, schema: dict) -> None:
    template = schema.get("episode_key")
    if template and all(lookup(fields, path) is not None for path in variables(template)):
        fields["$episode"] = render(template, fields)


def pre_transform_fields(fields: dict, transforms: Iterable[str] | None = None) -> dict:
    """Return a shallow copy for key templates. Only configured paths with a .raw companion are restored."""
    view = fields.copy()
    for path in transforms or ():
        raw_path = path + ".raw"
        if raw_path in fields:
            view[path] = fields[raw_path]
    return view


def _identity_fields(fields: dict, schema: dict, scales: dict[str, Decimal],
                     severity: Severity, occurred_at: datetime | None) -> dict:
    view = pre_transform_fields(fields, scales)
    # Rebuild derived aliases from raw inputs only where their source was transformed.
    aliases = schema.get("aliases", {})
    if (path := aliases.get("severity")) in scales:
        severity = _severity(view.get(path), schema)
    if (path := aliases.get("time")) in scales:
        occurred_at = _time(view.get(path), schema.get("time_format", "iso"))
    _set_labels(view, schema, severity, occurred_at)
    _set_episode(view, schema)
    return view


def normalize(raw: dict, source: dict, schemas: Sequence[dict] | SchemaDetector, *, received_at: datetime,
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


def reclassify(event: CanonicalEvent, schemas: Sequence[dict] | SchemaDetector) -> CanonicalEvent:
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
    original.pop("$episode", None)
    original.update(metadata)
    schema = detect_schema(original, schemas) or {}
    scales = transform_scales(schema["transforms"]) if "transforms" in schema else {}
    _apply_scales(original, scales)

    aliases = schema.get("aliases", {})
    severity = _severity(original.get(aliases.get("severity")), schema)
    occurred_at = _time(original.get(aliases.get("time")), schema.get("time_format", "iso"))
    original.update(metadata)
    private_keys = _identity_fields(original, schema, scales, severity, occurred_at)
    source_event_id = identity(message_id, schema, private_keys, source, digest)
    trace = dict(trace or {})
    if not schema and not (message_id and message_id.strip()) and source.get("dedup") == "payload_hash":
        trace["identity_method"] = "payload_hash"

    fields = flatten(public)
    fields.pop("$episode", None)
    _apply_scales(fields, scales)
    fields.update(metadata)
    public_keys = _identity_fields(fields, schema, scales, severity, occurred_at)
    _set_labels(fields, schema, severity, occurred_at)
    if "$episode" in public_keys:
        fields["$episode"] = public_keys["$episode"]
    template = schema.get("event_id")
    if template and source_event_id and not (message_id and message_id.strip()):
        private_key = render(template, private_keys)
        if source_event_id == storage_key(private_key) and private_key != render(template, public_keys):
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
