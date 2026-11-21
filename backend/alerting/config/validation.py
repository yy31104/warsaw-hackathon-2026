"""Validate API drafts against the data consumed by the deterministic core."""

import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from alerting.contracts import Channel
from alerting.rules.dsl import compile as compile_match
from alerting.rules.engine import duration
from alerting.runbooks.actions import REGISTRY

ObjectId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Owner = Annotated[str, StringConstraints(pattern=r"^(user|team):[^\s]+$")]
PositiveDuration = Annotated[str, StringConstraints(pattern=r"^[0-9]+[smhd]$")]
Severity = Literal["debug", "info", "warning", "error", "critical"]
RunbookFormat = Literal["yaml-steps", "markdown", "checklist", "text", "url"]


def validate_json(value):
    # PostgreSQL JSONB cannot represent NUL, unpaired surrogates or non-finite numbers.
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Config must contain valid Unicode text") from exc
    except (TypeError, RecursionError) as exc:
        raise ValueError("Config must contain JSON-compatible data") from exc
    if re.search(r"(?<!\\)(?:\\\\)*\\u0000", encoded):
        raise ValueError("Config text cannot contain NUL")
    return value


def validate_match(value):
    try:
        compile_match(value)
    except (TypeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid rule condition") from exc
    return value


class ConfigDraft(BaseModel):
    id: ObjectId
    version: int | None = Field(default=None, ge=1)
    origin: str | None = None
    source: Literal["human", "llm", "heuristic"] | None = None

    def spec(self):
        return self.model_dump(mode="json", exclude_none=True, exclude={"version", "origin", "source"})

    @model_validator(mode="after")
    def database_json(self):
        validate_json(self.spec())
        return self


class RuleRoute(BaseModel):
    to: Annotated[str, StringConstraints(pattern=r"^(user|team|oncall):[^\s]+$")]
    channels: Annotated[list[Channel], Field(min_length=1)] | Literal["preferences"] = "preferences"


class RulePresentation(BaseModel):
    title: str | None = None
    body: str | None = None
    fields: list[str] | None = None
    color: str | None = None


class RuleGroup(BaseModel):
    model_config = ConfigDict(extra="allow")
    by: list[str] | None = None
    mode: Literal["immediate", "digest"] | None = None
    cooldown: PositiveDuration | None = None
    every: PositiveDuration | None = None
    stable_for: PositiveDuration | None = None
    renotify_on_severity_increase: bool | None = None

    @field_validator("cooldown", "every", "stable_for")
    @classmethod
    def check_duration(cls, value, info):
        if value is not None:
            try:
                seconds = duration(value).total_seconds()
            except OverflowError as exc:
                raise ValueError("Duration is too large") from exc
            if info.field_name == "every" and seconds <= 0:
                raise ValueError("Digest interval must be positive")
        return value


class RuleThrottle(BaseModel):
    per_recipient: int = Field(ge=1)
    per: PositiveDuration

    @field_validator("per")
    @classmethod
    def check_duration(cls, value):
        try:
            seconds = duration(value).total_seconds()
        except OverflowError as exc:
            raise ValueError("Duration is too large") from exc
        if seconds <= 0:
            raise ValueError("Throttle interval must be positive")
        return value


class RuleDraft(ConfigDraft):
    model_config = ConfigDict(extra="allow")
    owner: Owner
    enabled: bool = True
    description: str | None = None
    plain_english: str | None = None
    matches_last_hour: int | None = None
    match: dict
    route: list[RuleRoute] = Field(min_length=1)
    present: RulePresentation | None = None
    group: RuleGroup | None = None
    throttle: RuleThrottle | None = None
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    shadow: bool | None = None

    @field_validator("match")
    @classmethod
    def check_match(cls, value):
        return validate_match(value)

    def spec(self):
        return self.model_dump(
            mode="json", exclude_none=True,
            exclude={"version", "origin", "source", "plain_english", "matches_last_hour", "warnings", "problems"},
        )


def describe_match(node):
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        terms = [describe_match(child) for child in node[key]]
        conjunction = " and " if key == "all" else " or "
        return f"({conjunction.join(terms)})" if terms else ("any event" if key == "all" else "no event")
    if "not" in node:
        return f"not ({describe_match(node['not'])})"
    if "field" not in node:
        return " and ".join(f"{k} = {v}" for k, v in node.items()) or "any event"
    op = node["op"]
    operator = {"eq": "=", "neq": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}.get(op, op)
    return f"{node['field']} {operator}" + (f" {node.get('value')}" if "value" in node else "")


def describe_rule(rule):
    if rule.get("description"):
        return rule["description"]
    targets = []
    for route in rule.get("route", []):
        channels = route.get("channels", "preferences")
        via = "their preferences" if channels == "preferences" else " + ".join(channels)
        targets.append(f"{route['to']} via {via}")
    try:
        condition = describe_match(rule.get("match", {}))
    except (KeyError, TypeError, RecursionError):
        condition = "the configured conditions match"
    return f"When {condition}, notify {', '.join(targets)}." if targets else f"Matches when {condition}."


class Preference(BaseModel):
    when: Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")] | None = None
    min_severity: Severity
    channels: list[Channel] = Field(min_length=1)


class OnCall(BaseModel):
    primary: ObjectId
    secondary: ObjectId | None = None


class TeamDraft(ConfigDraft):
    name: str | None = None
    members: list[ObjectId]
    oncall: OnCall | None = None
    approvers: list[ObjectId] = Field(default_factory=list)
    required_channels: dict[Severity, list[Channel]] | None = None
    preferences: list[Preference] | None = None


def is_link(content):
    if re.fullmatch(r"https?://\S+", content, re.IGNORECASE) is None:
        return False
    try:
        return bool(urlsplit(content).hostname)
    except ValueError:
        return False


class RunbookDraft(ConfigDraft):
    format: RunbookFormat | None = None
    content: str
    rules: list[ObjectId] | None = None
    matches: dict | None = None
    owner: Owner | None = None

    @field_validator("matches")
    @classmethod
    def check_matches(cls, value):
        return validate_match(value) if value is not None else None

    @model_validator(mode="after")
    def check_content(self):
        content = self.content.strip()
        if not content:
            raise ValueError("Runbook content is required")
        structured = self.format == "yaml-steps" or (
            self.format is None and re.search(r"^\s*steps\s*:", content, re.MULTILINE)
        )
        if structured:
            document = self.yaml_document()
            steps = document.get("steps")
            if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
                raise ValueError("Runbook steps must be a YAML list of objects")
            criteria = document.get("matches")
            if criteria is not None:
                validate_match(criteria)
            for step in steps:
                action = step.get("action")
                if action is not None and not isinstance(action, str):
                    raise ValueError("Runbook action must be a name")
                approval = step.get("approval")
                if "approval" in step and approval not in ("none", "required"):
                    raise ValueError("Approval must be none or required")
                handler = REGISTRY.get(action)
                if approval == "none" and handler and handler.default_approval == "required":
                    raise ValueError(f"{action} requires approval")
            validate_json(document)
            self.format = "yaml-steps"
        elif self.format is None:
            if is_link(content):
                self.format = "url"
            elif re.search(r"^\s*(?:[-*]\s+)?\[[ xX]\]\s", content, re.MULTILINE):
                self.format = "checklist"
            elif re.search(r"^\s*(?:#+\s|\d+[.)]\s)", content, re.MULTILINE):
                self.format = "markdown"
            else:
                self.format = "text"
        if self.format == "url" and not is_link(content):
            raise ValueError("Runbook URL must be an HTTP(S) link")
        return self

    def yaml_document(self):
        try:
            document = yaml.safe_load(self.content)
        except yaml.YAMLError as exc:
            raise ValueError("Invalid YAML runbook") from exc
        if not isinstance(document, dict):
            raise ValueError("YAML runbook must be an object with steps")
        return document

    def has_global_match(self):
        return bool(self.matches or (self.format == "yaml-steps" and self.yaml_document().get("matches")))
