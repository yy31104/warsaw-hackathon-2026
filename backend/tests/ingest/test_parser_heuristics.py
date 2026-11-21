from pathlib import Path

import pytest

from alerting.assist.parser_heuristics import heuristic_candidates
from alerting.ingest.normalize import InvalidEvent, parse_log_line


def test_logfmt_handles_quoted_values_and_new_keys_without_baking_in_values():
    lines = [
        'station=blue note="door | still open" reading=0',
        "note='valve has closed' station=green extra.flag=ok",
    ]
    parser = next(iter(heuristic_candidates(lines)))
    assert parser["field_names"] == [parser["kv_group"]]
    assert "blue" not in parser["pattern"]
    assert parse_log_line(lines[0], parser) == {"station": "blue", "note": "door | still open", "reading": "0"}
    assert parse_log_line('note="a=b c" new_field=99', parser) == {"note": "a=b c", "new_field": "99"}
    with pytest.raises(InvalidEvent):
        parse_log_line("free text", parser)


def test_public_log_fixture_preserves_prefix_and_expands_pairs():
    path = Path(__file__).resolve().parents[3] / "fixtures/events/pricing-engine.log"
    lines = path.read_text(encoding="utf-8").splitlines()
    parser = next(iter(heuristic_candidates(["unstructured outlier", *lines])))
    assert parser["field_names"] == ["timestamp", "level", "service", parser["kv_group"]]
    records = [parse_log_line(line, parser) for line in lines]
    assert records[0] == {
        "timestamp": "2026-10-23T09:14:01.002Z", "level": "INFO", "service": "pricing-engine-1",
        "instrument": "EURUSD", "latency_ms": "120", "msg": "quote published",
    }


@pytest.mark.parametrize(("line", "prefix"), [
    ("INFO collector phase=done", {"level": "INFO", "service": "collector"}),
    ("warn phase=late", {"level": "warn"}),
    ("collector phase=done", {"prefix": "collector"}),
    ("2028-04-01T05:06:07Z phase=done", {"timestamp": "2028-04-01T05:06:07Z"}),
])
def test_logfmt_accepts_partial_prefixes(line, prefix):
    parser = next(iter(heuristic_candidates([line])))
    record = parse_log_line(line, parser)
    assert record == {**prefix, "phase": line.split("=")[-1]}


def test_logfmt_key_can_share_the_prefix_field_name_without_losing_either_value():
    lines = ["INFO agent probe=one", "WARN agent level=critical"]
    parser = next(iter(heuristic_candidates(lines)))
    assert parse_log_line(lines[1], parser) == {
        "prefix_level": "WARN", "service": "agent", "level": "critical",
    }


def test_escaped_and_quoted_fragments_survive_physical_line_endings():
    line = r'''room=bay\ two kind="left\"door" status=pre'pending review'post''' + "\r\n"
    parser = next(iter(heuristic_candidates([line])))
    assert parse_log_line(line, parser) == {
        "room": "bay two", "kind": 'left"door', "status": "prepending reviewpost",
    }


@pytest.mark.parametrize("delimiter", ["|", "\t", ",", ";"])
def test_plain_delimiters_preserve_values_and_require_the_same_column_count(delimiter):
    lines = [delimiter.join(values) for values in [
        ["north", "baseline reading", "7"], ["south", "rising", "9"],
    ]]
    parser = next(iter(heuristic_candidates(["unstructured outlier", *lines])))
    assert parser["kv_group"] is None
    assert parser["field_names"] == ["field_1", "field_2", "field_3"]
    assert parse_log_line(delimiter.join(["west", "steady now", "8"]), parser) == {
        "field_1": "west", "field_2": "steady now", "field_3": "8",
    }
    with pytest.raises(InvalidEvent):
        parse_log_line(delimiter.join(["west", "steady now", "8", "extra"]), parser)


def test_width_uses_the_available_pattern_budget():
    line = ",".join(f"v{i}" for i in range(20))
    parser = next(iter(heuristic_candidates([line])))
    assert len(parser["pattern"]) <= 512
    assert parse_log_line(line, parser)["field_20"] == "v19"
    assert list(heuristic_candidates([",".join(["v"] * 70)])) == []


@pytest.mark.parametrize("lines", [
    [],
    ["a completely unstructured sentence", "another sentence about a pump"],
    ["the pump stopped, please try again"],
    ['one,"two,inside",three'],
    ["signal=active stray-word"],
    ['INFO collector signal="unclosed'],
])
def test_ambiguous_or_malformed_input_produces_no_candidate(lines):
    assert list(heuristic_candidates(lines)) == []
