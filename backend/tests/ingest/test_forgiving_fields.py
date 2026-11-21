"""M7: deterministic time and severity projection, with explicit rank approval."""
from datetime import UTC, datetime, timedelta, timezone

import pytest

from alerting.ingest.inputs import prepare
from alerting.ingest.normalize import normalize


AT = datetime(2026, 10, 23, 9, tzinfo=UTC)
SOURCE = {"id": "test", "transport": "http"}
SCHEMA = {
    "id": "m7-fields",
    "name": "case.m7",
    "detect": {"field": "level", "op": "exists"},
    "aliases": {"severity": "level", "time": "clock"},
    "severity_default": "info",
}


@pytest.mark.parametrize(("level", "expected"), [
    ("WARN", "warning"), ("warning", "warning"), ("crit", "critical"), ("fAtAl", "critical"),
    ("EMERG", "critical"), ("err", "error"), ("ERROR", "error"), ("critical", "critical"),
    ("NOTICE", "info"), ("info", "info"), ("Debug", "debug"),
])
def test_equivalent_severity_words_map_automatically(level, expected):
    event = normalize({"level": level}, SOURCE, [SCHEMA], received_at=AT)
    assert event.severity == expected and event.fields["$severity"] == expected
    assert event.trace["normalization"]["severity"] == "auto-detected"
    assert "problems" not in event.trace


@pytest.mark.parametrize("level", ["SEV1", "sev2", "P1", "p2", "HIGH", "medium", "low"])
def test_ranks_stay_unknown_until_confirmed(level):
    event = normalize({"level": level}, SOURCE, [SCHEMA], received_at=AT)
    assert event.severity == "unknown" and event.lane == "normal"
    (problem,) = event.trace["problems"]
    assert (problem["code"], problem["path"]) == ("unmapped_severity", "level") and problem["fix_example"]
    confirmed = {**SCHEMA, "severity_map": {level.upper(): "warning"}}
    reviewed = normalize({"level": level}, SOURCE, [confirmed], received_at=AT)
    assert reviewed.severity == "warning" and "problems" not in reviewed.trace


@pytest.mark.parametrize(("timestamp", "no_zone"), [
    (AT.isoformat(), False),
    (AT.astimezone(timezone(timedelta(hours=2))).isoformat(), False),
    ("2026-10-23T09:00:00Z", False),
    (int(AT.timestamp()), False),
    (int(AT.timestamp()) * 1000, False),
    (str(int(AT.timestamp()) * 1000), False),
    ("2026-10-23 09:00:00", True),
])
def test_time_format_inference(timestamp, no_zone):
    event = normalize({"level": "ERR", "clock": timestamp}, SOURCE, [SCHEMA], received_at=AT)
    assert event.occurred_at == AT and event.trace["normalization"]["time"] == "auto-detected"
    problems = event.trace.get("problems", [])
    assert [problem["code"] for problem in problems] == (["no_timezone"] if no_zone else [])
    if no_zone:
        assert problems[0]["path"] == "clock" and problems[0]["fix_example"].endswith("Z")


def test_explicit_clock_and_severity_config_takes_precedence():
    schema = {**SCHEMA, "severity_map": {"ERR": "warning"}, "time_format": "epoch_ms"}
    event = normalize({"level": "ERR", "clock": 1000}, SOURCE, [schema], received_at=AT)
    assert event.occurred_at.timestamp() == 1 and event.severity == "warning"
    assert "normalization" not in event.trace


def test_logfile_json_lines_and_existing_parser_share_normalization():
    source = {"id": "log", "transport": "logfile", "parser": {"type": "regex", "pattern": r"(?P<level>\w+)"}}
    for line in (b'{"level": "ERR"}\n', b"ERR\n"):
        event = prepare(line, source, [SCHEMA], received_at=AT)
        assert event.raw == {"level": "ERR"} and event.severity == "error"


async def test_verify_reports_no_timezone_and_unmapped_rank_then_shows_confirmation(client, monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    raw = {"service": "station", "level": "P1", "timestamp": "2026-10-23 09:00:00"}
    suggestion = await client.post("/v1/schemas/suggest", json={"sample": raw})
    assert suggestion.status_code == 200, suggestion.text
    data = suggestion.json()
    proposal = data["suggested_severity_map"]
    assert proposal == {"status": "suggested", "mapping": {"P1": "critical"}}
    assert data["draft"]["aliases"]["severity"] == "level" and data["draft"]["severity_map"] == {}
    draft = {**data["draft"], "time_format": None}

    async def verify(level, schema):
        response = await client.post("/v1/schemas/verify",
                                     json={"schema": schema, "samples": [{"event": {**raw, "level": level},
                                                                         "expect": "quiet"}]})
        assert response.status_code == 200, response.text
        return response.json()["samples"][0]

    before = await verify("P1", draft)
    assert before["fields"]["$severity"] == "unknown"
    assert {problem["code"] for problem in before["problems"]} == {"unmapped_severity", "no_timezone"}
    assert before["normalization"] == {"time": "auto-detected"}
    after = await verify("P1", {**draft, "severity_map": proposal["mapping"]})
    assert after["fields"]["$severity"] == "critical"
    assert [problem["code"] for problem in after["problems"]] == ["no_timezone"]
    automatic = await verify("FaTaL", draft)
    assert automatic["fields"]["$severity"] == "critical"
    assert automatic["normalization"] == {"severity": "auto-detected", "time": "auto-detected"}
