from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

import pytest

from alerting.assist import parser_validation as validation


def test_preview_uses_logfile_fullmatch_and_real_logfmt_expansion():
    pattern = r"(?P<source>\w+)\s+(?P<pairs>.+)"
    lines = [
        'station note="door open" seq=1\r\n', "station seq=2", "station seq=3",
        "station seq=4", 'station note="unfinished',
    ]
    with ThreadPoolExecutor(max_workers=1) as pool:
        rows, reason = pool.submit(
            validation.validate_candidate, pattern, ["pairs", "source"], "pairs", lines
        ).result()
    assert reason is None
    assert rows == [
        {"source": "station", "note": "door open", "seq": "1"},
        {"source": "station", "seq": "2"}, {"source": "station", "seq": "3"},
        {"source": "station", "seq": "4"}, None,
    ]


def test_coverage_is_computed_from_whole_lines_and_preserves_order():
    pattern = r"seq=(?P<seq>\d+)"
    rows, reason = validation.validate_candidate(
        pattern, ["seq"], None, ["seq=02", "prefix seq=5", "seq=3", "seq=4", "seq=5 tail"]
    )
    assert rows == [{"seq": "02"}, None, {"seq": "3"}, {"seq": "4"}, None]
    assert "80%" in reason


@pytest.mark.parametrize(("pattern", "names", "kv_group"), [
    ("(", ["slot"], None), (r"\d+", [], None),
    (r"(?P<slot>\w+)", ["slot", "extra"], None),
    (r"(?P<slot>\w+)", ["slot", "slot"], None),
    (r"(?P<slot>\w+)(:)?", ["slot"], None),
    (r"(?P<slot>\w+)", [], None),
    (r"(?P<slot>\w+)", ["slot"], "missing"),
])
def test_invalid_syntax_or_declarations_fail_closed(pattern, names, kv_group):
    rows, reason = validation.validate_candidate(pattern, names, kv_group, ["sample"])
    assert rows == [None]
    assert reason


@pytest.mark.parametrize(("pattern", "missing"), [
    (r"item:(?P<value>\w*)", "item:"),
    (r"item:(?P<value>x)?", "item:"),
    (r"item:(?P<value>\w+)?", "item:"),
])
def test_an_empty_or_absent_capture_rejects_even_at_eighty_percent(pattern, missing):
    rows, reason = validation.validate_candidate(pattern, ["value"], None, ["item:x"] * 4 + [missing])
    assert rows == [{"value": "x"}] * 4 + [None]
    assert "empty" in reason.lower()


@pytest.mark.parametrize("pattern", [
    r"(?P<sensor>\w+):(?P=sensor)",
    r"(?P<sensor>\w+)\b.*",
    r"(?P<sensor>\s+).*\b$",
    r"(?P<sensor>(?:north|west)+)",
    r"(?P<sensor>\w{1,10000})",
    r"(?P<sensor>\w+)(?P<station>\w+)",
])
def test_unsupported_complexity_is_rejected_before_starting_a_worker(pattern, monkeypatch):
    def unexpected_worker(*args):
        pytest.fail("The structural check must run before sample matching")

    monkeypatch.setattr(validation.multiprocessing, "get_context", unexpected_worker)
    names = ["sensor", "station"] if "station" in pattern else ["sensor"]
    rows, reason = validation.validate_candidate(pattern, names, None, ["north"])
    assert rows == [None]
    assert reason


def test_preview_can_return_a_field_larger_than_the_ipc_buffer():
    sample = "m" * (256 * 1024)
    rows, reason = validation.validate_candidate(r"\A(?P<note>.+)\Z", ["note"], None, [sample])
    assert reason is None
    assert rows == [{"note": sample}]


def test_busy_validator_has_a_bounded_admission_wait(monkeypatch):
    monkeypatch.setattr(validation, "_SLOTS", BoundedSemaphore(0))
    monkeypatch.setattr(validation, "_ADMISSION_SECONDS", 0.01)
    rows, reason = validation.validate_candidate(r"(?P<sensor>\w+)", ["sensor"], None, ["bay"])
    assert rows == [None]
    assert "busy" in reason.lower()


def _stalled_worker(connection, pattern, kv_group):
    # Wait for the parent without evaluating any expensive expression.
    with connection:
        connection.send(None)
        connection.recv()
        connection.recv()


def test_stalled_line_times_out_and_subsequent_validation_works(monkeypatch):
    args = (r"(?P<sensor>\w+)", ["sensor"], None, ["bay"])
    with monkeypatch.context() as patch:
        patch.setattr(validation, "_worker", _stalled_worker)
        patch.setattr(validation, "_LINE_SECONDS", 0.02)
        rows, reason = validation.validate_candidate(*args)
    assert rows == [None]
    assert "timed out" in reason.lower()
    assert validation.validate_candidate(*args) == ([{"sensor": "bay"}], None)
