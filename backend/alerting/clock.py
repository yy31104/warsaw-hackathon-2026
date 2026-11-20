"""Clock. SHARED FILE.

Time-dependent logic (preferences windows, cooldowns, digests) takes a Clock, never datetime.now().
When DEMO_MODE=1, an event may carry trace.demo_clock (from the X-Demo-Clock header) and
`clock_for_event` returns a fixed clock at that instant. Used by golden tests and the Anna demo.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, at: datetime):
        self._at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._at


SYSTEM = SystemClock()


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "0") == "1"


def clock_for_event(trace: dict[str, Any] | None):
    if demo_mode() and trace and trace.get("demo_clock"):
        return FixedClock(datetime.fromisoformat(str(trace["demo_clock"])))
    return SYSTEM
