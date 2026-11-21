"""Verify suggested parsers with the logfile reader, outside the API process."""
import multiprocessing
import re
from contextlib import ExitStack
from re import _compiler, _parser
from threading import BoundedSemaphore
from time import monotonic

from alerting.ingest.normalize import InvalidEvent, parse_log_line

MAX_PATTERN = 512
_MAX_EXPLICIT_REPEAT = 512
_LINE_SECONDS = 0.05
_STARTUP_SECONDS = 5
_ADMISSION_SECONDS = 0.5
_SLOTS = BoundedSemaphore(2)
_SINGLE = {_parser.LITERAL, _parser.NOT_LITERAL, _parser.IN, _parser.ANY}
_REPEAT = {_parser.MAX_REPEAT, _parser.MIN_REPEAT, _parser.POSSESSIVE_REPEAT}
_START_ANCHORS = {_parser.AT_BEGINNING, _parser.AT_BEGINNING_STRING}
_END_ANCHORS = {_parser.AT_END, _parser.AT_END_STRING}
# With ASCII literals/ranges, Unicode categories can only distinguish four
# additional kinds of character: space, decimal, other word, and non-word.
_ALPHABET = frozenset(map(chr, range(128))) | frozenset("　٣é☃")


def _alphabet(state, atom):
    op, arg = atom
    members = arg if op is _parser.IN else [atom]
    for kind, value in members:
        if kind in {_parser.LITERAL, _parser.NOT_LITERAL} and value > 127:
            return _ALPHABET
        if kind is _parser.RANGE and value[1] > 127:
            return _ALPHABET
    matcher = _compiler.compile(_parser.SubPattern(state, [atom]), state.flags)
    return {char for char in _ALPHABET if matcher.fullmatch(char)}


def _pieces(tokens):
    for op, arg in tokens:
        if op is _parser.SUBPATTERN:
            _, add_flags, del_flags, body = arg
            if add_flags or del_flags:
                raise ValueError
            yield from _pieces(body)
        elif op in _SINGLE:
            yield (op, arg), 1, 1, False
        elif op in _REPEAT:
            lo, hi, body = arg
            if lo > _MAX_EXPLICIT_REPEAT or (hi != _parser.MAXREPEAT and hi > _MAX_EXPLICIT_REPEAT):
                raise ValueError
            inner = list(_pieces(body))
            if len(inner) != 1 or len(body) != 1:
                raise ValueError
            atom, inner_min, inner_max, _ = inner[0]
            if body.getwidth() == (1, 1):
                yield atom, lo, hi, op is _parser.POSSESSIVE_REPEAT
            elif (lo, hi) == (0, 1) and inner_min > 0 and inner_max > 1:
                # An optional capture of one nonempty character run, e.g. (\w+)?.
                yield atom, 0, inner_max, False
            else:
                raise ValueError
        elif op is _parser.BRANCH:
            # Literal alternatives with distinct initials have a single possible branch.
            branches = arg[1]
            if not all(b and all(k is _parser.LITERAL for k, _ in b) for b in branches):
                raise ValueError
            initials = [b[0] for b in branches]
            if len(set(initials)) != len(initials):
                raise ValueError
            yield (_parser.IN, initials), 1, 1, False
        else:
            raise ValueError


def _safe_pattern(pattern):
    parsed = _parser.parse(pattern, 0)
    if parsed.state.flags & re.IGNORECASE:
        return False
    tokens = list(parsed)
    if tokens and tokens[0][0] is _parser.AT and tokens[0][1] in _START_ANCHORS:
        tokens.pop(0)
    if tokens and tokens[-1][0] is _parser.AT and tokens[-1][1] in _END_ANCHORS:
        tokens.pop()
    try:
        pieces = list(_pieces(tokens))
    except ValueError:
        return False
    # A variable-width run needs a delimiter it cannot consume. A terminal
    # wildcard can consume the rest of any physical log line. Possessive
    # repetitions already commit to one boundary.
    for i, (atom, lo, hi, possessive) in enumerate(pieces[:-1]):
        if lo == hi or possessive:
            continue
        consumed = _alphabet(parsed.state, atom)
        for j in range(i + 1, len(pieces)):
            following, minimum, maximum, _ = pieces[j]
            if j == len(pieces) - 1 and following[0] is _parser.ANY and maximum == _parser.MAXREPEAT:
                break
            if consumed & _alphabet(parsed.state, following):
                return False
            if minimum:
                break
    return True


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
    if len(pattern) > MAX_PATTERN:
        return rows, f"The parser pattern exceeds {MAX_PATTERN} characters."
    try:
        compiled = re.compile(pattern)
        names = compiled.groupindex.keys()
        if (not names or compiled.groups != len(names)
                or names != set(field_names) or len(names) != len(field_names)):
            return rows, "Declare each named capture exactly once in field_names."
        if kv_group is not None and kv_group not in names:
            return rows, "kv_group must name a declared capture."
        if not _safe_pattern(pattern):
            return rows, "Use simple character classes and unambiguous separators in the parser."
    except (ValueError, re.error, OverflowError, RecursionError):
        return rows, "The parser pattern could not be compiled as a Python regular expression."

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
