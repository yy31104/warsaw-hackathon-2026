"""Time fixed synthetic logfile records locally with no services.

Run from the root with:
    PYTHONPATH=backend uv run --offline --project backend python tools/bench_regex_worst.py
"""

import platform
import re
from pathlib import Path
from time import perf_counter_ns

import yaml

from alerting.ingest.regex_safety import MAX_LINE_BYTES, is_safe_pattern, safe_fullmatch


REPEATS = 200
LIMIT_NS = 20_000_000
SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config/sources.yaml"


def record(prefix="", *, fill="x", suffix=""):
    """Pad to the live parser's exact UTF-8 byte limit before timing."""
    room = MAX_LINE_BYTES - len((prefix + suffix).encode("utf-8"))
    count, remainder = divmod(room, len(fill.encode("utf-8")))
    if count < 0 or remainder:
        raise ValueError("Fixture padding does not fit the byte limit.")
    return prefix + fill * count + suffix


def late_miss(prefix="", *, fill="x", suffix=":7", wrong_last="!"):
    """Make equal-length records that differ only in the last byte."""
    return (
        (record(prefix, fill=fill, suffix=suffix), True),
        (record(prefix, fill=fill, suffix=suffix[:-1] + wrong_last), False),
    )


def cases():
    yield "Anchors, literals and named capture", r"\Areading=(?P<value>x+):7\Z", late_miss("reading=")
    yield "One-atom noncapturing repetition", r"(?:x)+:7", late_miss()
    yield "Quantified numbered capture", r"(x)+:7", late_miss()
    yield "Positive class and range", r"[x-zQ]+:7", late_miss(fill="Q")
    yield "Negated literal with UTF-8 and CRLF", r"[^:]+:[0-9]", (
        (record(fill="é", suffix=":7\r\n"), True),
        (record(fill="é", suffix=":!\r\n"), False),
    )
    yield "Negated class", r"[^:!]+:7", late_miss()
    yield "Unicode word, space and decimal classes", r"\w+\s\d\d\d", late_miss(fill="é", suffix=" 777")
    yield "Complementary categories", r"\S+\s\D\W", late_miss(suffix=" x.", wrong_last="x")
    yield "Zero-or-more and optional", r"x*:(y)?:7", late_miss(suffix=":y:7") + late_miss(suffix="::7")
    yield "Fixed repetition at 512", r"y{512}:x+:7", late_miss("y" * 512 + ":")
    yield "Bounded lazy repetition", r"y{1,512}?:x+?:7", late_miss("y" * 512 + ":")
    yield "Possessive repetition with an overlapping atom", r"x*+x?:7", late_miss()
    yield "Literal alternatives", r"(ok|warn):x+:7", late_miss("ok:") + late_miss("warn:")
    yield "Terminal wildcard", r"x+:.*", (
        (record("x:", fill="y"), True),
        (record(suffix="!"), False),
    )
    yield "Pattern length at 300 characters", "y" * 297 + "x+7", late_miss("y" * 297, suffix="7")
    yield "Capture count at 20", "(y)" * 19 + "(x+):7", late_miss("y" * 19)
    yield "Compiled ASCII flag", re.compile(r"\w+:7", re.ASCII), late_miss()
    yield "Compiled DOTALL and MULTILINE flags", re.compile(r"^x+:.*$", re.S | re.M), (
        (record("x:", fill="y"), True),
        (record(suffix="!"), False),
    )
    yield "Inline VERBOSE flag", r"(?x) ^ (x+):7 $", late_miss()

    shipped_samples = {
        "pricing-logs": (
            (record("2030-01-01T00:00:00Z INFO bench price=", fill="7"), True),
            (record("2030-01-01T00:00:00Z ", fill="I", suffix=" bench price=7"), True),
            (record("2030-01-01T00:00:00Z ", fill="I", suffix="!"), False),
        ),
    }
    for source in yaml.safe_load(SOURCE_CONFIG.read_text(encoding="utf-8"))["sources"]:
        if source["transport"] == "logfile" and "parser" in source:
            yield "Shipped " + source["id"], source["parser"]["pattern"], shipped_samples[source["id"]]


def measure(pattern, samples):
    max_ns = 0
    for line, should_match in samples:
        if len(line.encode("utf-8")) != MAX_LINE_BYTES:
            raise ValueError("Sample is not exactly the configured byte limit.")
        for _ in range(REPEATS):
            start = perf_counter_ns()
            match = safe_fullmatch(pattern, line)
            elapsed_ns = perf_counter_ns() - start
            if (match is not None) != should_match:
                raise ValueError("Sample produced an unexpected match result.")
            max_ns = max(max_ns, elapsed_ns)
    return max_ns


def main():
    print(f"Python {platform.python_version()}, {platform.system()} {platform.machine()}. "
          f"{MAX_LINE_BYTES:,} bytes per record. {REPEATS} calls per record.")
    print()
    print("| Pattern | Samples (match / miss) | Max ms per call | 20 ms budget |")
    print("| --- | ---: | ---: | --- |")
    failed = False
    for name, pattern, samples in cases():
        # Measure the first safety lookup as well as cached calls. Validate
        # the text form after timing to avoid warming the safety cache.
        max_ns = measure(pattern, samples)
        accepted, reason = is_safe_pattern(pattern if isinstance(pattern, str) else pattern.pattern)
        if not accepted:
            raise ValueError(f"{name}: {reason}")
        passed = max_ns <= LIMIT_NS
        failed |= not passed
        matches = sum(expected for _, expected in samples)
        print(f"| {name} | {matches} / {len(samples) - matches} | "
              f"{max_ns / 1_000_000:.3f} | {'PASS' if passed else 'FAIL'} |")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
