import re
from pathlib import Path

import pytest

from alerting.config import files
from alerting.ingest.normalize import InvalidEvent, parse_log_line
from alerting.ingest.regex_safety import is_safe_pattern, safe_fullmatch


@pytest.mark.parametrize("pattern", [
    r"(x+)+",
    r"(x*)*",
    r"(.*)+",
    r"(?P<reading>\d+)?",
    r"(?:(?:x{2}){3})",
    r"(?:(?:x{1}){1})",
    r"(?P<station>\w+):(?P=station)",
    r"(ab):\1",
    r"(?<=a)b",
    r"(?<!a)b",
    "(",
])
def test_unsupported_syntax_has_a_reason_before_any_sample_is_matched(pattern):
    safe, reason = is_safe_pattern(pattern)
    assert safe is False
    assert isinstance(reason, str) and reason.strip()


@pytest.mark.parametrize(("at_limit", "extra"), [
    pytest.param("é" * 300, "x", id="300-characters"),
    pytest.param("".join(f"(?P<f{i}>x)" for i in range(20)), "(y)", id="20-captures"),
])
def test_exact_limit_is_allowed_and_one_more_is_rejected(at_limit, extra):
    assert is_safe_pattern(at_limit) == (True, None)
    safe, reason = is_safe_pattern(at_limit + extra)
    assert safe is False
    assert isinstance(reason, str) and reason.strip()


def test_safe_fullmatch_returns_the_pricing_groups_and_none_on_a_nonmatch():
    parser = next(source["parser"] for source in files.sources() if source["id"] == "pricing-logs")
    path = Path(__file__).resolve().parents[3] / "fixtures/events/pricing-engine.log"
    line = path.read_text(encoding="utf-8").splitlines()[0]
    assert is_safe_pattern(parser["pattern"]) == (True, None)
    match = safe_fullmatch(parser["pattern"], line)
    assert isinstance(match, re.Match)
    assert match.groupdict() == {
        "timestamp": "2026-10-23T09:14:01.002Z", "level": "INFO", "instance": "pricing-engine-1",
        "kv": 'instrument=EURUSD latency_ms=120 msg="quote published"',
    }
    assert safe_fullmatch(parser["pattern"], "ordinary prose") is None
    assert parse_log_line(line, parser)["msg"] == "quote published"


def test_unsafe_pattern_raises_the_ingest_error():
    pattern = "x" * 301
    with pytest.raises(InvalidEvent):
        safe_fullmatch(pattern, pattern)
    with pytest.raises(InvalidEvent):
        parse_log_line(pattern, {"pattern": pattern})


def test_utf8_limit_is_inclusive_and_size_is_checked_before_regex(monkeypatch):
    pattern = r"(?P<reading>.+)"
    line = "é" * (8 * 1024)
    assert len(line.encode("utf-8")) == 16 * 1024
    match = safe_fullmatch(pattern, line)
    assert match is not None and match["reading"] == line
    assert parse_log_line(line, {"pattern": pattern}) == {"reading": line}

    def unexpected_analysis(*args, **kwargs):
        pytest.fail("Oversized input reached the regex engine")

    monkeypatch.setattr("alerting.ingest.regex_safety._analyze", unexpected_analysis)
    for oversized in (line + "x", "x" * (256 * 1024), "x" * (256 * 1024 + 1)):
        with pytest.raises(InvalidEvent, match=r"^line too long for the configured parser$"):
            safe_fullmatch(pattern, oversized)
    for oversized in (line + "x", "x" * (256 * 1024)):
        with pytest.raises(InvalidEvent, match=r"^line too long for the configured parser$"):
            parse_log_line(oversized, {"pattern": pattern})
    with pytest.raises(InvalidEvent, match=r"^log line exceeds 256 KiB$"):
        parse_log_line("x" * (256 * 1024 + 1), {"pattern": pattern})
