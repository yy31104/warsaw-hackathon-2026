"""Shared types. SHARED FILE — change only via a contract PR (docs/BRIEF.md §7).

Mirrors docs/CONTRACT.md. Keep it import-light: pydantic + stdlib only.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


# ---------- enums ----------
class Severity(str, Enum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"
    unknown = "unknown"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.debug: 0,
    Severity.info: 1,
    Severity.warning: 2,
    Severity.error: 3,
    Severity.critical: 4,
}  # Severity.unknown is deliberately absent: not comparable.


class Channel(str, Enum):
    email = "email"
    sms = "sms"
    phone = "phone"
    ui = "ui"


class Outcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NO_MATCH = "NO_MATCH"
    NEW_ALERT = "NEW_ALERT"
    EXISTING_ALERT = "EXISTING_ALERT"
    SUPPRESSED_COOLDOWN = "SUPPRESSED_COOLDOWN"
    SUPPRESSED_THROTTLE = "SUPPRESSED_THROTTLE"
    DIGEST_QUEUED = "DIGEST_QUEUED"
    NOTIFIED = "NOTIFIED"
    UNROUTED = "UNROUTED"
    RESOLVED = "RESOLVED"
    STALE_RESOLVE = "STALE_RESOLVE"
    QUARANTINED = "QUARANTINED"
    RULE_FAULT = "RULE_FAULT"
    SILENCED = "SILENCED"


class Transition(str, Enum):
    created = "created"
    occurrence = "occurrence"
    severity_increased = "severity_increased"
    cooldown_elapsed = "cooldown_elapsed"
    digest_due = "digest_due"
    resolved = "resolved"
    escalation = "escalation"


Transport = Literal["http", "rabbitmq", "logfile"]
Lane = Literal["critical", "normal"]


# ---------- events ----------
class CanonicalEvent(BaseModel):
    id: int | None = None
    source_id: str
    transport: Transport
    source_event_id: str | None = None
    payload_hash: str
    schema_id: str | None = None
    schema_version: int | None = None
    schema_name: str | None = None
    severity: Severity = Severity.unknown
    service: str | None = None
    environment: str | None = None
    occurred_at: datetime | None = None
    received_at: datetime
    raw: dict[str, Any]
    fields: dict[str, Any] = Field(default_factory=dict)  # flattened paths + "$alias" keys
    lane: Lane = "normal"
    status: Literal["pending", "processed", "needs_review", "rejected", "quarantined"] = "pending"
    trace: dict[str, Any] = Field(default_factory=dict)  # gen_sent_at, received_at, demo_clock, ...


# ---------- alerts ----------
class AlertSnapshot(BaseModel):
    id: int
    rule_id: str
    rule_version: int
    alert_key: str
    status: Literal["open", "acknowledged", "resolved"]
    severity: Severity
    priority: str | None = None
    score: float | None = None
    lane: Lane = "normal"
    team_id: str | None = None
    title: str | None = None
    body: str | None = None
    color: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    group_values: dict[str, Any] = Field(default_factory=dict)
    runbook_id: str | None = None
    occurrence_count: int = 0
    count_since_notify: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_notified_at: datetime | None = None
    representative_event_id: int | None = None


# ---------- notifications ----------
class RenderedMessage(BaseModel):
    title: str
    body: str
    short: str  # <= 160 chars, for SMS / phone
    color: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    runbook_excerpt: str | None = None
    link: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # alert_id, gen_sent_at, rule_id, ...


class NotificationIntent(BaseModel):
    alert_id: int
    kind: Literal["initial", "update", "digest", "escalation", "approval_request", "throttle_summary"]
    recipient: str  # user id, or "team:<id>" for the ui channel
    channel: Channel
    contact: str | None = None
    rendered: RenderedMessage
    priority: int = 3  # 1 = highest
    dedup_key: str


class Plan(BaseModel):
    """Return value of routing.plan(). The rule-worker inserts `intents` and writes `runbook_id` on the alert."""
    intents: list[NotificationIntent] = Field(default_factory=list)
    runbook_id: str | None = None
    unrouted_reason: str | None = None  # e.g. "no recipient resolved" -> decision UNROUTED


class SendResult(BaseModel):
    outcome: Literal["delivered", "retryable", "permanent", "unknown"]
    provider_ref: str | None = None
    error: str | None = None
    retry_after_s: float | None = None


class ChannelProvider(Protocol):
    channel: Channel

    async def send(self, msg: RenderedMessage, contact: str, idempotency_key: str) -> SendResult: ...


# ---------- runbook actions ----------
class ActionContext(BaseModel):
    alert: AlertSnapshot
    event_fields: dict[str, Any] = Field(default_factory=dict)
    actor: str
    run_id: int | None = None


class ActionResult(BaseModel):
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    resolves_alert: bool = False


class Action(Protocol):
    name: str
    default_approval: Literal["none", "required"]
    params_schema: dict[str, Any]

    async def execute(self, params: dict[str, Any], ctx: ActionContext) -> ActionResult: ...


# ---------- misc ----------
class Clock(Protocol):
    def now(self) -> datetime: ...


class SSEMessage(BaseModel):
    type: Literal[
        "alert.created",
        "alert.updated",
        "notification.updated",
        "action.updated",
        "schema.issue",
        "config.changed",
        "stats.tick",
    ]
    data: dict[str, Any]
