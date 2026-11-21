"""Decode an HTTP body into records before transport identity and normalization."""
import csv
import re
from dataclasses import dataclass
from datetime import date
from io import StringIO
from xml.etree.ElementTree import ParseError

import yaml
from defusedxml import ElementTree as safe_xml
from defusedxml.common import DefusedXmlException

from alerting.ingest.normalize import MAX_BYTES, MAX_DEPTH, MAX_PATHS, InvalidEvent, decode_json, parse_log_line
from alerting.templates import lookup


FIX_EXAMPLES = {
    "json": '{"kind":"metric"}',
    "ndjson": '{"kind":"metric"}\n{"kind":"status"}',
    "csv": 'kind,message\nmetric,"High, check sensor"',
    "yaml": "kind: metric",
    "xml": "<event><kind>metric</kind></event>",
    "text": 'For a key=value parser, send a line such as level=ERROR message="Check sensor".',
}
MEDIA_TYPES = {
    "application/json": "json", "text/json": "json",
    "application/x-ndjson": "ndjson", "application/ndjson": "ndjson",
    "application/jsonl": "ndjson", "application/x-jsonl": "ndjson",
    "application/jsonlines": "ndjson", "application/x-jsonlines": "ndjson",
    "text/ndjson": "ndjson", "text/x-ndjson": "ndjson", "text/jsonl": "ndjson",
    "text/csv": "csv", "application/csv": "csv",
    "application/yaml": "yaml", "application/x-yaml": "yaml",
    "text/yaml": "yaml", "text/x-yaml": "yaml",
    "application/xml": "xml", "text/xml": "xml",
    "text/plain": "text",
}
CONTENT_TYPE_HINT = "Send Content-Type: text/csv for CSV, or set the media type matching your body."
_CSV_HEADER = re.compile(r"[^\W\d][\w.@#-]*")

# The HTTP request already has a byte cap. Permit a single CSV cell up to that cap.
csv.field_size_limit(max(csv.field_size_limit(), MAX_BYTES))


class BodyError(InvalidEvent):
    def __init__(self, reason, format=None, *, status=400, field="body", line=None, tried=()):
        super().__init__(reason)
        self.format = format
        self.status = status
        self.field = field
        self.line = line
        self.tried = tried
        self.fix = FIX_EXAMPLES.get(format, CONTENT_TYPE_HINT)


@dataclass(frozen=True)
class Row:
    value: object = None
    line: int | None = None
    reason: str | None = None
    field: str = "body"


@dataclass(frozen=True)
class Body:
    format: str
    rows: list[Row]
    batch: bool


def media_format(content_type):
    media = (content_type or "").partition(";")[0].strip().lower()
    return MEDIA_TYPES.get(media) or next(
        (name for name in ("json", "xml", "yaml") if media.endswith("+" + name)), None
    )


def _read_json(body):
    try:
        return decode_json(body)
    except InvalidEvent as exc:
        raise BodyError(str(exc), "json") from exc


def _json_body(payload, source):
    if isinstance(payload, dict) and (path := source.get("split_path")):
        children = lookup(payload, path)
        if children is not None or path in payload:
            if not isinstance(children, list):
                raise BodyError("split_path must contain an array", "json", field=path)
            payload = children
    batch = isinstance(payload, list)
    return Body("json", [Row(item) for item in payload] if batch else [Row(payload)], batch)


def _lines(text):
    return ((number, line) for number, line in enumerate(text.splitlines(), 1) if line.strip())


def _ndjson(text):
    rows = []
    for number, line in _lines(text):
        try:
            item = decode_json(line.encode())
            if not isinstance(item, dict):
                raise InvalidEvent("NDJSON lines must contain JSON objects")
            rows.append(Row(item, number))
        except InvalidEvent as exc:
            rows.append(Row(line=number, reason=str(exc)))
    if not rows:
        raise BodyError("NDJSON contains no records", "ndjson", line=1)
    return Body("ndjson", rows, True)


def _csv(text, *, sniff=False):
    reader = csv.reader(StringIO(text, newline=""), strict=True)
    try:
        header = next((row for row in reader if row), [])
    except csv.Error as exc:
        raise BodyError("Invalid CSV header quoting", "csv", field="header", line=reader.line_num) from exc
    names = [name.strip() for name in header]
    if not names or not all(names) or len(set(names)) != len(names) or any("\0" in name for name in names):
        raise BodyError("CSV requires a header with distinct, non-empty column names", "csv",
                        field="header", line=reader.line_num or 1)
    if sniff and (len(names) < 2 or not all(_CSV_HEADER.fullmatch(name) for name in header)):
        raise BodyError("CSV header is ambiguous", "csv", field="header", line=reader.line_num)
    rows = []
    while True:
        line = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error:
            rows.append(Row(line=line, reason="Invalid CSV quoting"))
            continue
        if not cells:
            continue
        if len(cells) != len(names):
            rows.append(Row(line=line, reason=f"Expected {len(names)} columns, got {len(cells)}"))
        else:
            rows.append(Row(dict(zip(names, cells)), line))
    if not rows:
        raise BodyError("CSV contains no data rows", "csv", line=reader.line_num + 1)
    return Body("csv", rows, True)


def _child_path(path, key):
    return f"{path}.{key}" if path else str(key)


def _yaml_record(value):
    if not isinstance(value, dict):
        raise BodyError("YAML records must be mappings", "yaml")
    remaining = MAX_PATHS + 1  # The root mapping is not counted by flatten().

    def convert(item, depth, path):
        nonlocal remaining
        remaining -= 1
        field = path or "body"
        if remaining < 0:
            raise BodyError("event exceeds 2000 paths", "yaml", field=field)
        if depth > MAX_DEPTH:
            raise BodyError("event exceeds depth 20", "yaml", field=field)
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise BodyError("YAML mapping keys must be strings", "yaml", field=field)
            return {key: convert(child, depth + 1, _child_path(path, key)) for key, child in item.items()}
        if isinstance(item, list):
            return [convert(child, depth + 1, _child_path(path, index)) for index, child in enumerate(item)]
        if isinstance(item, date):
            return item.isoformat()
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise BodyError("YAML value cannot be stored as JSON", "yaml", field=field)

    return convert(value, 0, "")


def _yaml(text):
    try:
        # Use the same SafeLoader as safe_load, retaining the record start marks
        # from this parse instead of composing the YAML document a second time.
        loader = yaml.SafeLoader(text)
        try:
            node = loader.get_single_node()
            payload = loader.construct_document(node) if node is not None else None
        finally:
            loader.dispose()
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        raise BodyError("Invalid YAML. Send a single mapping or list of mappings", "yaml",
                        line=mark.line + 1 if mark else 1) from exc
    batch = isinstance(payload, list)
    if not batch and not isinstance(payload, dict):
        line = node.start_mark.line + 1 if node is not None else 1
        raise BodyError("YAML body must be a mapping or list of mappings", "yaml", line=line)
    rows = []
    records = zip(payload, node.value, strict=True) if batch else [(payload, node)]
    for item, record in records:
        line = record.start_mark.line + 1
        try:
            rows.append(Row(_yaml_record(item), line))
        except BodyError as exc:
            rows.append(Row(line=line, reason=str(exc), field=exc.field))
    return Body("yaml", rows, batch)


def _xml_record(element, depth=0, path=""):
    if depth > MAX_DEPTH:
        raise BodyError("event exceeds depth 20", "xml", field=path or "body")
    fields = {"@" + name: value for name, value in element.attrib.items()}
    children = {}
    for child in element:
        children.setdefault(child.tag, []).append(_xml_record(child, depth + 1, _child_path(path, child.tag)))
    fields.update({key: values[0] if len(values) == 1 else values for key, values in children.items()})
    text = ((element.text or "") + "".join(child.tail or "" for child in element)).strip()
    if text:
        fields["#text"] = text
    return fields


def _xml(text):
    try:
        root = safe_xml.fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise BodyError("XML DTDs, entities and external references are prohibited", "xml") from exc
    except (ParseError, ValueError) as exc:
        position = getattr(exc, "position", (1, 0))
        raise BodyError("Invalid XML", "xml", line=position[0]) from exc
    # Explicit collection roots identify batches; repeated children in an event are data.
    batch = root.tag.rsplit("}", 1)[-1] in {"events", "records"} and not root.attrib and not (
        (root.text or "").strip() or any((child.tail or "").strip() for child in root)
    )
    rows = []
    for element in root if batch else [root]:
        try:
            rows.append(Row(_xml_record(element)))
        except BodyError as exc:
            rows.append(Row(reason=str(exc), field=exc.field))
    return Body("xml", rows, batch)


def _plain(text, source):
    rows = []
    parser = source.get("parser")
    for number, line in _lines(text):
        try:
            fields = parse_log_line(line, parser) if parser else {"message": line}
            rows.append(Row(fields, number))
        except InvalidEvent as exc:
            rows.append(Row(line=number, reason=str(exc)))
    if not rows:
        raise BodyError("Plain text contains no events", "text", line=1)
    return Body("text", rows, True)


_DECODERS = {"ndjson": _ndjson, "csv": _csv, "yaml": _yaml, "xml": _xml}


def _sniff(body):
    tried = ["json"]
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError as exc:
        raise BodyError("Unrecognized input encoding", status=415, tried=tried) from exc

    # These signatures are disjoint. YAML without a document marker and bare
    # text can describe the same bytes, so they require an explicit media type.
    signature = text.lstrip()
    format = None
    if signature.startswith(("{", "[")):
        format = "ndjson"
    elif signature.startswith("<"):
        format = "xml"
    elif signature.split("\n", 1)[0].strip() == "---" or signature.startswith("%YAML "):
        format = "yaml"
    elif "," in signature.split("\n", 1)[0]:
        format = "csv"
    if format:
        tried.append(format)
        try:
            decoded = _csv(text, sniff=True) if format == "csv" else _DECODERS[format](text)
        except BodyError:
            pass
        else:
            if all(row.reason is None for row in decoded.rows):
                return decoded
    raise BodyError("Cannot identify a supported format unambiguously", status=415, field="Content-Type", tried=tried)


def _decode_declared(body, format, source):
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError as exc:
        raise BodyError("Body must be encoded as UTF-8", format) from exc
    if format != "text":
        return _DECODERS[format](text)
    if text.lstrip().startswith("{"):
        ndjson = _ndjson(text)
        if all(row.reason is None for row in ndjson.rows):
            return ndjson
    return _plain(text, source)


def decode_body(body, content_type, source):
    format = media_format(content_type)
    if format == "json":
        return _json_body(_read_json(body), source)

    # Legacy clients send JSON with arbitrary headers, including curl's default.
    # A successfully decoded, explicitly declared CSV/XML/YAML body takes precedence.
    try:
        payload = decode_json(body)
    except InvalidEvent:
        payload = None
    if isinstance(payload, (dict, list)):
        if format in {"csv", "xml", "yaml"}:
            try:
                declared = _decode_declared(body, format, source)
            except BodyError:
                pass
            else:
                if all(row.reason is None for row in declared.rows):
                    return declared
        return _json_body(payload, source)

    if not content_type or not content_type.strip():
        return _sniff(body)
    if format is None:
        raise BodyError("Unsupported Content-Type", status=415, field="Content-Type",
                        tried=[content_type.partition(";")[0].strip()])
    return _decode_declared(body, format, source)
