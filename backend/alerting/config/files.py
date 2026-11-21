"""Minimal file reader for config/*.yaml so every developer is unblocked from minute one.

daizhongtian (DEV2) replaces the *source of truth* with config_objects (versioned, hot-reloaded) behind the same
function names in alerting/config/loader.py; callers switch by changing one import.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from alerting.ingest.regex_safety import validate_source_patterns

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", Path(__file__).resolve().parents[3] / "config"))


def _load(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources() -> list[dict[str, Any]]:
    return validate_source_patterns(_load("sources.yaml").get("sources", []))


def schemas() -> list[dict[str, Any]]:
    return _load("schemas.yaml").get("schemas", [])


def rules() -> list[dict[str, Any]]:
    return _load("rules.yaml").get("rules", [])


def directory() -> dict[str, Any]:
    """teams.yaml as-is: fallback_team, default_preferences, teams, users."""
    return _load("teams.yaml")


def runbooks() -> list[dict[str, Any]]:
    """runbooks.yaml entries with `content` (original text) attached."""
    base = CONFIG_DIR / "runbooks"
    entries = _load("runbooks/runbooks.yaml").get("runbooks", [])
    for entry in entries:
        entry["content"] = (base / entry["file"]).read_text(encoding="utf-8")
    return entries
