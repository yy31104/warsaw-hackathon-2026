from alerting.ingest.logtail import MAX_BATCH, frame_lines
from alerting.ingest.normalize import MAX_BYTES


def test_framing_leaves_a_partial_crlf_and_utf8_line_unconsumed():
    first = b"first\r\n"
    partial = "next \N{EURO SIGN}".encode()[:-1]
    assert frame_lines(first + partial) == ([first], len(first), False)
    assert frame_lines(partial) == ([], 0, False)


def test_framing_rejects_an_oversized_record_once_even_at_intermediate_eof():
    longest = b"x" * (MAX_BYTES - 1) + b"\n"
    assert frame_lines(longest) == ([longest], MAX_BYTES, False)
    oversized = b"x" * (MAX_BYTES + 1)
    lines, offset, discarding = frame_lines(oversized)
    assert lines == ["log line exceeds 256 KiB"]
    assert offset == len(oversized) and discarding
    assert frame_lines(b"continued", discarding) == ([], 9, True)
    assert frame_lines(b"end\nnext\npartial", discarding) == ([b"next\n"], 9, False)


def test_framing_limits_records_per_batch():
    body = b"line\n" * (MAX_BATCH + 1)
    assert frame_lines(body) == ([b"line\n"] * MAX_BATCH, 5 * MAX_BATCH, False)
