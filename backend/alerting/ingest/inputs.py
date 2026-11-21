"""Common transport boundary for parsed JSON objects, broker bodies and log lines."""
from datetime import datetime

from alerting.clock import SYSTEM, demo_mode
from alerting.contracts import CanonicalEvent
from alerting.ingest.normalize import InvalidEvent, SchemaDetector, decode_json, normalize, parse_log_line
from alerting.ingest.store import RejectedInput


def prepare(payload, source: dict, schemas: list[dict] | SchemaDetector, *, message_id: str | None = None,
            headers=None, received_at: datetime | None = None) -> CanonicalEvent | RejectedInput:
    received_at = received_at or SYSTEM.now()
    headers = headers or {}
    trace = {}
    if demo_mode() and headers.get("x-demo-fault") == "raise":
        trace["demo_fault"] = "raise"
    try:
        trace["gen_sent_at"] = int(headers["x-gen-sent-at"])
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    try:
        trace["demo_clock"] = datetime.fromisoformat(str(headers["x-demo-clock"])).isoformat()
    except (KeyError, ValueError, OverflowError):
        pass
    try:
        if source["transport"] == "logfile":
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            if payload.lstrip().startswith("{"):
                payload = decode_json(payload.encode())
            else:
                payload = parse_log_line(payload, source["parser"])
        elif isinstance(payload, bytes):
            payload = decode_json(payload)
        return normalize(payload, source, schemas, received_at=received_at, message_id=message_id, trace=trace)
    except (InvalidEvent, UnicodeError) as exc:
        return RejectedInput(source["id"], message_id, str(exc))
