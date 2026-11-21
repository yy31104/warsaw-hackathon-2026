"""Verify suggested parsers with the logfile reader, outside the API process."""
import multiprocessing
import re
from contextlib import ExitStack
from threading import BoundedSemaphore
from time import monotonic

from alerting.ingest.normalize import InvalidEvent, parse_log_line
from alerting.ingest.regex_safety import MAX_PATTERN, check_line_size, is_safe_pattern

_LINE_SECONDS = 0.05
_STARTUP_SECONDS = 5
_ADMISSION_SECONDS = 0.5
_SLOTS = BoundedSemaphore(2)


def _worker(connection, pattern, kv_group):
    parser = {"pattern": re.compile(pattern)}
    with connection:
        connection.send(None)
        while True:
            try:
                line = connection.recv()
            except EOFError:
                return
            try:
                # Inspect captures before logfmt removes its group or overwrites
                # a field. Both operations run under the same per-line deadline.
                fields = parse_log_line(line, parser)
                empty = any(not value or not value.strip() for value in fields.values())
                if empty:
                    fields = None
                elif kv_group:
                    fields = parse_log_line(line, {**parser, "kv_group": kv_group})
            except (InvalidEvent, UnicodeError):
                fields, empty = None, False
            connection.send((fields, empty))


def validate_candidate(pattern: str, field_names: list[str], kv_group: str | None,
                       lines: list[str]) -> tuple[list[dict[str, str] | None], str | None]:
    rows = [None] * len(lines)
    if not lines:
        return rows, "Provide at least one sample line."
    valid, reason = is_safe_pattern(pattern)
    if not valid:
        return rows, reason
    try:
        for line in lines:
            check_line_size(line)
    except (InvalidEvent, UnicodeError) as exc:
        return rows, str(exc)
    compiled = re.compile(pattern)
    names = compiled.groupindex.keys()
    if (not names or compiled.groups != len(names)
            or names != set(field_names) or len(names) != len(field_names)):
        return rows, "Declare each named capture exactly once in field_names."
    if kv_group is not None and kv_group not in names:
        return rows, "kv_group must name a declared capture."

    empty_capture = False
    with ExitStack() as cleanup:
        if not _SLOTS.acquire(timeout=_ADMISSION_SECONDS):
            return rows, "Parser validation is busy. Retry shortly."
        cleanup.callback(_SLOTS.release)
        context = multiprocessing.get_context("spawn")
        try:
            parent, child = context.Pipe()
        except OSError:
            return rows, "Parser validation could not start."
        cleanup.enter_context(parent)
        cleanup.enter_context(child)
        process = context.Process(target=_worker, args=(child, pattern, kv_group), daemon=True)
        try:
            process.start()
            child.close()
            if not parent.poll(_STARTUP_SECONDS):
                return rows, "Parser validation did not start in time."
            parent.recv()
            for index, line in enumerate(lines):
                deadline = monotonic() + _LINE_SECONDS
                parent.send(line)
                if not parent.poll(max(0, deadline - monotonic())) or monotonic() > deadline:
                    return rows, "Parser validation timed out on a sample line."
                rows[index], empty = parent.recv()
                empty_capture |= empty
        except (EOFError, OSError):
            return rows, "Parser validation could not finish."
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.kill()
                process.join()
            process.close()

    if empty_capture:
        return rows, "A named capture was empty on a matching line."
    if 5 * sum(row is not None for row in rows) < 4 * len(rows):
        return rows, "The parser must match at least 80% of the sample lines."
    return rows, None
