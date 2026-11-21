"""Shared limits for configured Python logfile patterns and physical records."""
import re
from functools import lru_cache
from re import _compiler, _parser

MAX_PATTERN = 300
MAX_GROUPS = 20
MAX_LINE_BYTES = 16 * 1024
_MAX_EXPLICIT_REPEAT = 512
_SINGLE = {_parser.LITERAL, _parser.NOT_LITERAL, _parser.IN, _parser.ANY}
_REPEAT = {_parser.MAX_REPEAT, _parser.MIN_REPEAT, _parser.POSSESSIVE_REPEAT}
_START_ANCHORS = {_parser.AT_BEGINNING, _parser.AT_BEGINNING_STRING}
_END_ANCHORS = {_parser.AT_END, _parser.AT_END_STRING}
# ASCII literals/ranges and Python's character categories distinguish four
# additional Unicode classes: space, decimal, other word, and non-word.
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


def _pieces(tokens, in_repeat=False):
    for op, arg in tokens:
        if op is _parser.SUBPATTERN:
            _, add_flags, del_flags, body = arg
            if add_flags or del_flags:
                raise ValueError("Scoped regex flags are unsupported in logfile parsers.")
            yield from _pieces(body, in_repeat)
        elif op in _SINGLE:
            yield (op, arg), 1, 1, False
        elif op in _REPEAT:
            if in_repeat:
                raise ValueError("Nested quantifiers are unsupported in logfile parsers.")
            lo, hi, body = arg
            if lo > _MAX_EXPLICIT_REPEAT or (hi != _parser.MAXREPEAT and hi > _MAX_EXPLICIT_REPEAT):
                raise ValueError(f"Explicit repetition bounds must be at most {_MAX_EXPLICIT_REPEAT}.")
            inner = list(_pieces(body, in_repeat=True))
            if len(inner) != 1 or body.getwidth() != (1, 1):
                raise ValueError("A quantifier may repeat only a single character or character class.")
            yield inner[0][0], lo, hi, op is _parser.POSSESSIVE_REPEAT
        elif op is _parser.BRANCH:
            branches = arg[1]
            if not all(b and all(k is _parser.LITERAL for k, _ in b) for b in branches):
                # Report a disallowed construct inside an alternative by its name.
                for branch in branches:
                    list(_pieces(branch, in_repeat))
                raise ValueError("Alternatives must be nonempty literal strings with distinct initials.")
            initials = [b[0] for b in branches]
            if len(set(initials)) != len(initials):
                raise ValueError("Alternatives must have distinct initial characters.")
            yield (_parser.IN, initials), 1, 1, False
        elif op in {_parser.GROUPREF, _parser.GROUPREF_EXISTS}:
            raise ValueError("Backreferences are unsupported in logfile parsers.")
        elif op in {_parser.ASSERT, _parser.ASSERT_NOT}:
            name = "Lookbehind" if arg[0] < 0 else "Lookahead"
            raise ValueError(f"{name} assertions are unsupported in logfile parsers.")
        elif op is _parser.AT:
            raise ValueError("Only start and end anchors at the boundaries of the pattern are supported.")
        else:
            raise ValueError("Use literal fields, character classes, and unambiguous separators.")


def _check_structure(parsed):
    if parsed.state.flags & re.IGNORECASE:
        raise ValueError("Case-insensitive flags are unsupported in logfile parsers.")
    tokens = list(parsed)
    if tokens and tokens[0][0] is _parser.AT and tokens[0][1] in _START_ANCHORS:
        tokens.pop(0)
    if tokens and tokens[-1][0] is _parser.AT and tokens[-1][1] in _END_ANCHORS:
        tokens.pop()
    pieces = list(_pieces(tokens))
    # Each variable-width run needs a delimiter it cannot consume. A terminal
    # wildcard can consume the remainder of a physical log line.
    for i, (atom, lo, hi, possessive) in enumerate(pieces[:-1]):
        if lo == hi or possessive:
            continue
        consumed = _alphabet(parsed.state, atom)
        for j in range(i + 1, len(pieces)):
            following, minimum, maximum, _ = pieces[j]
            if j == len(pieces) - 1 and following[0] is _parser.ANY and maximum == _parser.MAXREPEAT:
                break
            if consumed & _alphabet(parsed.state, following):
                raise ValueError("Use an unambiguous separator after a variable-width field.")
            if minimum:
                break


@lru_cache(maxsize=256)
def _analyze(pattern, flags):
    try:
        compiled = re.compile(pattern, flags)
        parsed = _parser.parse(pattern, flags)
    except (ValueError, re.error, OverflowError, RecursionError):
        return None, "Invalid Python regular expression syntax."
    if compiled.groups > MAX_GROUPS:
        return None, f"The parser pattern exceeds {MAX_GROUPS} capture groups."
    try:
        _check_structure(parsed)
    except ValueError as exc:
        return None, str(exc)
    return compiled, None


def _checked(pattern, flags=0):
    # Reject oversized inputs before they become cache keys.
    if not isinstance(pattern, str):
        return None, "The parser pattern must be a string."
    if len(pattern) > MAX_PATTERN:
        return None, f"The parser pattern exceeds {MAX_PATTERN} characters."
    if flags & re.DEBUG:
        return None, "The regex debug flag is unsupported in logfile parsers."
    return _analyze(pattern, flags)


def is_safe_pattern(pattern: str) -> tuple[bool, str | None]:
    """Check the configured parser grammar without running the pattern on data."""
    _, reason = _checked(pattern)
    return reason is None, reason


def validate_source_patterns(sources: list[dict]) -> list[dict]:
    for source in sources:
        if source.get("transport") != "logfile" or "parser" not in source:
            continue
        parser = source["parser"]
        pattern = parser.get("pattern") if isinstance(parser, dict) else None
        valid, reason = is_safe_pattern(pattern)
        if not valid:
            raise ValueError(f"Invalid parser for source {source.get('id')!r}. {reason}")
    return sources


def _invalid_event(reason):
    # normalize imports safe_fullmatch and owns the transport's exception type.
    from alerting.ingest.normalize import InvalidEvent

    return InvalidEvent(reason)


def check_line_size(line: str) -> None:
    """Count original UTF-8 bytes, including any physical line ending."""
    if len(line) > MAX_LINE_BYTES or (not line.isascii() and len(line.encode("utf-8")) > MAX_LINE_BYTES):
        raise _invalid_event("line too long for the configured parser")


def safe_fullmatch(pattern: str | re.Pattern[str], line: str) -> re.Match[str] | None:
    check_line_size(line)
    if isinstance(pattern, re.Pattern):
        # UNICODE is implicit for text. Reuse the same entry as a string pattern.
        compiled, reason = _checked(pattern.pattern, int(pattern.flags) & ~int(re.UNICODE))
    else:
        compiled, reason = _checked(pattern)
    if reason is not None:
        raise _invalid_event(reason)
    return compiled.fullmatch(line.rstrip("\r\n"))
