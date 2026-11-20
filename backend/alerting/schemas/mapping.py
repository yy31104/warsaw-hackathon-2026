"""Human-editable schema drafts and the review fingerprint defined in CONTRACT §3.6."""
import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from alerting.contracts import Severity
from alerting.rules.dsl import compile as compile_match

Text = Annotated[str, StringConstraints(min_length=1)]
Alias = Literal["service", "severity", "environment", "error_code", "message", "time"]


def fingerprint(raw: dict) -> str:
    # Ordinary keys retain the human-readable spelling used by the UI fixture.
    keys = [key.replace("%", "%25").replace(",", "%2C") or "%00" for key in sorted(raw)]
    label = ",".join(keys) or "%empty"
    if len(label.encode()) > 512:
        return "%hash:" + hashlib.sha256(label.encode()).hexdigest()
    return label


def _check_groups(node, depth=0):
    if not isinstance(node, dict) or not node or depth > 20:
        raise ValueError("expected a nonempty condition, up to 20 levels deep")
    if "op" in node and not isinstance(node["op"], str):
        raise ValueError("a condition operator must be a string")
    for group in ("all", "any"):
        if group in node:
            if not isinstance(node[group], list) or not node[group]:
                raise ValueError("a condition group must contain at least one child")
            for child in node[group]:
                _check_groups(child, depth + 1)
    if "not" in node:
        _check_groups(node["not"], depth + 1)


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

    @field_validator("detect", "resolve_when")
    @classmethod
    def validate_detector(cls, value):
        if value is not None:
            _check_groups(value)
            compile_match(value)
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
