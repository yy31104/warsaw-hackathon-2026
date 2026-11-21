"""Infer logfile parser candidates from logfmt records or plain delimited columns."""
import re
from collections import Counter
from collections.abc import Iterator

from alerting.ingest.regex_safety import MAX_PATTERN

_KEY = r"\w[\w.-]*"
_TIMESTAMP = r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}\S*"
_LEVELS = "trace|debug|info|notice|warn|warning|err|error|crit|critical|fatal|alert|emerg|emergency"
_KV = _KEY + "=.*"
_PREFIX = {"timestamp": _TIMESTAMP, "level": "[A-Za-z]+", "service": r"[^\s=]+"}
_LOGFMT = re.compile(
    rf"\s*(?:(?P<timestamp>{_TIMESTAMP})\s+)?(?:(?P<level>{_LEVELS})\s+)?"
    rf"(?:(?P<service>[^\s=]+)\s+)?(?P<kv>{_KV})", re.IGNORECASE,
)
# Scan key names without materializing long values. Quoted, escaped and ordinary
# fragments have distinct first characters. The live parser decodes their values.
_VALUE = r"""(?:[^'"\\ \t\r\n]++|'[^']*'|"(?:[^"\\]++|\\.)*+"|\\.)*+"""
_PAIR = re.compile(rf"(?P<key>{_KEY})={_VALUE}(?:[ \t\r\n]+|\Z)")
_DELIMITERS = (("\t", r"\t"), ("|", r"\|"), (";", ";"), (",", ","))


def _logfmt_layout(line: str):
    if "=" not in line:
        return None
    match = _LOGFMT.fullmatch(line)
    if not match:
        return None
    keys = set()
    offset = match.start("kv")
    while offset < len(line):
        pair = _PAIR.match(line, offset)
        if pair is None:
            return None
        keys.add(pair["key"])
        offset = pair.end()
    return tuple(role for role in _PREFIX if match[role]), keys


def _column_count(line: str, delimiter: str) -> int:
    size = line.count(delimiter) + 1
    # Even empty named group bodies would exceed the budget above this width.
    if not 2 <= size <= MAX_PATTERN // len("(?P<field_1>") or '"' in line:
        return 0
    cells = [cell.strip() for cell in line.split(delimiter)]
    if not all(cells):
        return 0
    # With only sentence fragments, a comma or semicolon can just be punctuation.
    if delimiter in ",;" and all(len(cell.split(None, 1)) > 1 for cell in cells):
        return 0
    return size


def heuristic_candidates(lines: list[str]) -> Iterator[dict]:
    """Try the most frequent prefix layout, then the most frequent width per delimiter."""
    layouts = Counter()
    keys = set()
    plain = []
    for entry in lines:
        line = entry.rstrip("\r\n")
        if layout := _logfmt_layout(line):
            prefix, tail_keys = layout
            layouts[prefix] += 1
            keys.update(tail_keys)
        else:
            plain.append(line)

    if layouts:
        prefix, _ = layouts.most_common(1)[0]
        names, groups = [], []
        for role in prefix:
            # The live parser expands pairs after capturing the header. Preserve both
            # values if the producer also sends e.g. a level= pair in the tail.
            name = "prefix" if prefix == ("service",) else role
            suffix = 1
            while name in keys:
                name = f"prefix_{role}" if suffix == 1 else f"prefix_{role}_{suffix}"
                suffix += 1
            names.append(name)
            groups.append(f"(?P<{name}>{_PREFIX[role]})")
        groups.append(f"(?P<kv>{_KV})")
        yield {"pattern": r"^\s*" + r"\s+".join(groups) + "$", "field_names": [*names, "kv"], "kv_group": "kv"}

    for delimiter, escaped in _DELIMITERS:
        counts = Counter(width for line in plain if (width := _column_count(line, delimiter)))
        for width, _ in counts.most_common():
            names = [f"field_{index}" for index in range(1, width + 1)]
            groups = [rf'(?P<{name}>[^{escaped}"\r\n]+)' for name in names]
            pattern = "^" + escaped.join(groups) + "$"
            if len(pattern) <= MAX_PATTERN:
                yield {"pattern": pattern, "field_names": names, "kv_group": None}
                break
