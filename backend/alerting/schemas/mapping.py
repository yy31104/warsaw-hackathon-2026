"""Human-editable schema drafts and the review fingerprint defined in CONTRACT §3.6."""
import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from alerting.contracts import Severity

Text = Annotated[str, StringConstraints(min_length=1)]
Alias = Literal["service", "severity", "environment", "error_code", "message", "time"]


def fingerprint(raw: dict) -> str:
    # Ordinary keys retain the human-readable spelling used by the UI fixture.
    keys = [key.replace("%", "%25").replace(",", "%2C") or "%00" for key in sorted(raw)]
    label = ",".join(keys) or "%empty"
    if len(label.encode()) > 512:
        return "%hash:" + hashlib.sha256(label.encode()).hexdigest()
    return label


def _check_detector(node, depth=0):
    if not isinstance(node, dict) or depth > 20:
        raise ValueError("expected a schema detection condition, up to 20 levels deep")
    for group in ("all", "any"):
        if set(node) == {group} and isinstance(node[group], list) and node[group]:
            for child in node[group]:
                _check_detector(child, depth + 1)
            return
    if set(node) == {"not"}:
        _check_detector(node["not"], depth + 1)
        return
    if isinstance(node.get("field"), str) and node["field"]:
        if set(node) == {"field", "op"} and node["op"] == "exists":
            return
        if set(node) == {"field", "op", "value"} and node["op"] == "eq":
            return
    raise ValueError("use a field with exists or eq, or combine conditions with all, any or not")


class SchemaDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[Text, Field(max_length=128)]
    name: Text
    detect: dict[str, Any]
    aliases: dict[Alias, Text] = Field(default_factory=dict)
    severity_map: dict[str, Severity] = Field(default_factory=dict)
    severity_default: Severity | None = None
    time_format: Literal["iso", "epoch_s", "epoch_ms"] | None = None
    event_id: Text | None = None
    alert_key: Text | None = None
    resolve_when: dict | None = None

    @field_validator("detect")
    @classmethod
    def validate_detector(cls, value):
        _check_detector(value)
        return value

    def spec(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)

    @model_validator(mode="after")
    def database_json(self):
        # JSONB rejects NUL, non-finite numbers and unpaired Unicode surrogates.
        encoded = json.dumps(self.model_dump(exclude_none=True), allow_nan=False, ensure_ascii=False)
        encoded.encode("utf-8")
        if re.search(r"(?<!\\)(?:\\\\)*\\u0000", encoded):
            raise ValueError("schema text cannot contain NUL")
        return self
