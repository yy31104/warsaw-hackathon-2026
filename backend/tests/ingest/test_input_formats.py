"""M6: real HTTP decoding, durable inputs and routing across representations."""
import asyncio
import csv
import json
import shlex
from datetime import datetime
from io import StringIO
from socket import AF_INET, SOCK_STREAM, socket
from xml.etree import ElementTree as xml

import httpx
import pytest
import uvicorn
import yaml

from alerting.api.app import app
from alerting.config import files
from alerting.rules.storage import close_settings
from alerting.rules.worker import process_batch


URL = "/v1/events/generic-webhook"
KEY = {"X-Api-Key": "demo-generic-key"}
AT = "2026-10-23T09:00:00Z"
TEXT = "Check, then retry"
ALERT = "NEEDS_CHECK"
QUIET = "HEALTHY"


@pytest.fixture
async def live_http(client):
    # Use the same FastAPI app and isolated PostgreSQL as client, over loopback HTTP.
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None))
    with socket(AF_INET, SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("HTTP test server exited before listening")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as connection:
                yield connection
        finally:
            server.should_exit = True
            await task


def _csv_text(columns, rows):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue().encode()


def _fixture(format):
    """Return decoded field paths and a pair of producer records in one format."""
    codes = (ALERT, QUIET)
    if format in {"json", "ndjson"}:
        records = [{"node": {"name": "station", "level": "ERR", "timestamp": AT},
                    "report": {"state": code, "summary": TEXT}, "deployment": "prod"} for code in codes]
        payload = json.dumps(records) if format == "json" else "\n".join(map(json.dumps, records))
        media = "application/json" if format == "json" else "application/x-ndjson"
        paths = ("node.name", "node.level", "node.timestamp", "report.state", "report.summary", "deployment")
        return media, payload.encode(), paths
    if format == "csv":
        paths = ("component", "level", "time", "result", "notes", "stage")
        data = _csv_text(paths, [("station", "error", "1792746000000", code, TEXT, "prod") for code in codes])
        return "text/csv", data, paths
    if format == "yaml":
        paths = ("device.name", "rank", "logged_at", "signal", "description", "zone")
        records = [{"device": {"name": "station"}, "rank": "ErR", "logged_at": datetime.fromisoformat(AT),
                    "signal": code, "description": TEXT, "zone": "prod"} for code in codes]
        return "application/yaml", yaml.safe_dump(records).encode(), paths
    if format == "xml":
        paths = ("@component", "@level", "@at", "code.#text", "details.#text", "@env")
        events = xml.Element("events")
        for code in codes:
            event = xml.SubElement(events, "event", component="station", level="ERROR", at=AT, env="prod")
            xml.SubElement(event, "code").text = code
            xml.SubElement(event, "details").text = TEXT
        return "application/xml", xml.tostring(events), paths
    paths = ("worker", "state_level", "happened", "result_code", "message", "environment")
    lines = "\n".join(f"worker=station state_level=err happened={AT} result_code={code} "
                      f"message={shlex.quote(TEXT)} environment=prod" for code in codes)
    return "text/plain", lines.encode(), paths


def _text_parser(monkeypatch):
    sources = files.sources()
    parser = {"type": "regex", "pattern": r"(?P<kv>.*)", "kv_group": "kv"}
    monkeypatch.setattr(files, "sources", lambda: [
        {**source, "parser": parser} if source["id"] == "generic-webhook" else source for source in sources
    ])


async def _write_rule(client, schema_name):
    rule = {
        "id": "m6-station", "owner": "team:settlements",
        "match": {"all": [
            {"field": "$schema", "op": "eq", "value": schema_name},
            {"field": "$error_code", "op": "eq", "value": ALERT},
            {"field": "$severity", "op": "eq", "value": "error"},
        ]},
        "route": [{"to": "user:sam", "channels": ["sms"]}, {"to": "team:settlements", "channels": ["ui"]}],
    }
    response = await client.post("/v1/rules", headers={"X-User": "lee"}, json=rule)
    assert response.status_code == 201, response.text


async def _routes(pool, event_id):
    async with pool.connection() as conn:
        return await (await conn.execute("""
            SELECT n.recipient, n.channel FROM notifications n
            JOIN alert_occurrences o ON o.alert_id = n.alert_id
            WHERE o.event_id = %s ORDER BY n.recipient, n.channel
        """, (event_id,))).fetchall()


ROUTES = [("sam", "sms"), ("team:settlements", "ui")]


@pytest.mark.parametrize("format", ("json", "ndjson", "csv", "yaml", "xml", "text"))
async def test_formats_route_the_same_mapped_business_fields(live_http, inbox_pool, monkeypatch, format):
    if format == "text":
        _text_parser(monkeypatch)
    media, body, paths = _fixture(format)
    field_names = ("service", "severity", "time", "error_code", "message", "environment")
    schema = {"id": "m6-station", "name": "station.status", "detect": {"field": paths[0], "op": "exists"},
              "aliases": dict(zip(field_names, paths, strict=True))}
    published = await live_http.post("/v1/schemas", headers={"X-User": "ops"}, json=schema)
    assert published.status_code == 200, published.text
    await _write_rule(live_http, schema["name"])
    try:
        response = await live_http.post(URL, content=body,
                                        headers={**KEY, "Content-Type": media, "Idempotency-Key": "proof"})
        assert response.status_code == 202, response.text
        assert response.json()["accepted"] == 2 and response.json()["rejected"] == []
        assert await process_batch(inbox_pool) == 2
        ids = response.json()["event_ids"]
        for index, (event_id, code) in enumerate(zip(ids, (ALERT, QUIET), strict=True)):
            event = (await live_http.get(f"/v1/events/{event_id}")).json()
            assert event["source_event_id"] == f"http-batch:{index}:proof"
            assert (event["status"], event["severity"]) == ("processed", "error")
            assert {key: event["fields"]["$" + key] for key in field_names} == {
                "service": "station", "severity": "error", "time": "2026-10-23T09:00:00+00:00",
                "error_code": code, "message": TEXT, "environment": "prod",
            }
            assert await _routes(inbox_pool, event_id) == (ROUTES if code == ALERT else [])
    finally:
        await close_settings(inbox_pool)


async def test_csv_leading_zeros_bom_crlf_quotes_and_batch_retry(client, inbox_pool):
    body = b'\xef\xbb\xbfdevice,note\r\n00123,"check, sensor"\r\n00007,"first line\r\nsecond line"\r\n'
    headers = {**KEY, "Content-Type": "text/csv; charset=utf-8", "Idempotency-Key": "csv-retry"}
    first = await client.post(URL, content=body, headers=headers)
    retry = await client.post(URL, content=body, headers=headers)
    assert first.status_code == retry.status_code == 202
    ids = first.json()["event_ids"]
    assert len(set(ids)) == 2
    assert retry.json() == {"accepted": 0, "duplicates": 2, "conflicts": 0, "rejected": [], "event_ids": ids}
    for event_id, value, note in zip(ids, ("00123", "00007"), ("check, sensor", "first line\r\nsecond line"), strict=True):
        stored = (await client.get(f"/v1/events/{event_id}")).json()
        assert stored["raw"] == {"device": value, "note": note}
    async with inbox_pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM events")).fetchone())[0] == 2


@pytest.mark.parametrize("body,expected", [
    (b'{"sensor":"a"}', 1),
    (b'{"sensor":"a"}\n\n{"sensor":"b"}', 2),
    (b'\xef\xbb\xbfsensor,site\r\n001,"lab, north"\r\n002,office\r\n', 2),
    (b"---\nsensor: metric\n", 1),
    (b"<event><sensor>metric</sensor></event>", 1),
])
async def test_unambiguous_without_content_type(client, body, expected):
    response = await client.post(URL, content=body, headers=KEY)
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == expected


@pytest.mark.parametrize("body", [b"a note in plain text", b"key: text", b"id,note\n001\r\n", b"hello, there\nhi, there"])
async def test_ambiguous_format_requires_explicit_media_type(client, body):
    response = await client.post(URL, content=body, headers=KEY)
    assert response.status_code == 415
    assert response.json()["tried"] and "Content-Type: text/csv" in response.json()["hint"]


async def test_content_type_selects_decoder(client):
    response = await client.post(URL, content=b"code,note\n1,example\n",
                                 headers={**KEY, "Content-Type": "application/json"})
    assert response.status_code == 400 and "invalid JSON" in response.json()["detail"]
    unsupported = await client.post(URL, content=b"code,note\n1,example\n",
                                    headers={**KEY, "Content-Type": "application/pdf"})
    assert unsupported.status_code == 415 and unsupported.json()["field"] == "Content-Type"


async def test_existing_json_is_identical_across_legacy_content_types(live_http):
    payload = {"device": {"id": "00123"}, "measurements": [4, 9], "message": "legacy JSON"}
    body = json.dumps(payload).encode()
    media_types = (None, "application/json", "text/plain", "application/octet-stream",
                   "application/x-www-form-urlencoded", "text/csv", "application/xml")
    stored = []
    for index, media in enumerate(media_types):
        key = f"legacy-{index}"
        headers = {**KEY, "Idempotency-Key": key}
        if media is not None:
            headers["Content-Type"] = media
        response = await live_http.post(URL, headers=headers, content=body)
        assert response.status_code == 202, (media, response.text)
        result = response.json()
        assert result["accepted"] == 1 and not result["rejected"]
        (event_id,) = result["event_ids"]
        event = (await live_http.get(f"/v1/events/{event_id}")).json()
        assert event["source_event_id"] == key and event["raw"] == payload
        stored.append({key: event[key] for key in ("raw", "fields", "severity", "status", "payload_hash")})
    assert all(event == stored[0] for event in stored)


async def test_json_array_keeps_batch_identity_under_curl_and_plain_text_headers(client):
    body = b'[{"metric":"one"},{"metric":"two"}]'
    headers = {**KEY, "Idempotency-Key": "legacy-array"}
    first = await client.post(URL, content=body, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    assert first.status_code == 202 and first.json()["accepted"] == 2
    retry = await client.post(URL, content=body, headers={**headers, "Content-Type": "text/plain"})
    assert retry.status_code == 202
    assert retry.json()["duplicates"] == 2 and retry.json()["event_ids"] == first.json()["event_ids"]


async def test_explicit_yaml_takes_precedence_for_a_body_that_also_parses_as_json(client):
    payload = {"alerts": [{"message": "one"}, {"message": "two"}]}
    response = await client.post("/v1/events/alertmanager", content=json.dumps(payload),
                                 headers={"X-Api-Key": "demo-alertmanager-key", "Content-Type": "application/yaml"})
    assert response.status_code == 202, response.text
    (event_id,) = response.json()["event_ids"]
    event = (await client.get(f"/v1/events/{event_id}")).json()
    assert event["raw"] == payload  # YAML keeps this wrapper. The JSON source splits it.


async def test_plain_text_uses_ndjson_only_when_all_nonempty_lines_are_objects(client):
    first_line = b'{"value":"alpha"}'
    second_line = b'{"value":"beta"}'
    headers = {**KEY, "Content-Type": "text/plain", "Idempotency-Key": "plain-objects"}
    response = await client.post(URL, content=first_line + b"\n\n" + second_line, headers=headers)
    assert response.status_code == 202 and response.json()["accepted"] == 2
    for index, event_id in enumerate(response.json()["event_ids"]):
        stored = (await client.get(f"/v1/events/{event_id}")).json()
        assert stored["raw"] == {"value": ("alpha", "beta")[index]}
        assert stored["source_event_id"] == f"http-batch:{index}:plain-objects"
    mixed = await client.post(URL, content=first_line + b"\nraw text", headers={**KEY, "Content-Type": "text/plain"})
    assert mixed.status_code == 202 and mixed.json()["accepted"] == 2
    for event_id, value in zip(mixed.json()["event_ids"], (first_line.decode(), "raw text"), strict=True):
        stored = (await client.get(f"/v1/events/{event_id}")).json()
        assert stored["raw"] == {"message": value}


async def test_yaml_records_report_the_start_line_and_field(client):
    response = await client.post(URL, content="---\n- sensor: first\n- nested:\n    7: wrong\n- sensor: third\n",
                                 headers={**KEY, "Content-Type": "application/yaml"})
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 2
    (rejection,) = response.json()["rejected"]
    assert (rejection["index"], rejection["line"], rejection["field"]) == (1, 3, "nested")


async def test_non_json_request_and_event_limits(client):
    oversized = await client.post(URL, content=b"message\n" + b"x" * (256 * 1024),
                                  headers={**KEY, "Content-Type": "text/csv"})
    assert oversized.status_code == 413 and "256 KiB" in oversized.json()["detail"]
    too_many_fields = await client.post(URL, content=_csv_text(
        [f"field{i}" for i in range(2001)], [["v"] * 2001]),
        headers={**KEY, "Content-Type": "text/csv"})
    assert too_many_fields.status_code == 400
    assert "2000 paths" in too_many_fields.json()["rejected"][0]["reason"]
    too_deep = await client.post(URL, content=b"<event>" + b"<a>" * 21 + b"x" + b"</a>" * 21 + b"</event>",
                                headers={**KEY, "Content-Type": "application/xml"})
    assert too_deep.status_code == 400
    (rejection,) = too_deep.json()["rejected"]
    assert rejection["index"] == 0 and rejection["field"].startswith("a.a.")
    assert "depth 20" in rejection["reason"] and "line" not in rejection


async def test_partial_ndjson_and_csv_preserve_line_and_batch_identity(client):
    cases = (
        ("application/jsonl", b'{"value":"alpha"}\n\nbroken\n42\n{"value":"beta"}\n', [3, 4], [1, 2], 3),
        ("text/csv", b'value,notes\r\nalpha,"two\r\nlines"\r\nbad\r\nbeta,works\r\n', [4], [1], 2),
    )
    for media, body, lines, indexes, last_index in cases:
        headers = {**KEY, "Content-Type": media, "Idempotency-Key": media}
        response = await client.post(URL, content=body, headers=headers)
        assert response.status_code == 202, response.text
        summary = response.json()
        assert summary["accepted"] == 2
        assert [row["line"] for row in summary["rejected"]] == lines
        assert [row["index"] for row in summary["rejected"]] == indexes
        assert all(row["reason"] and row["fix_example"] for row in summary["rejected"])
        stored = (await client.get(f"/v1/events/{summary['event_ids'][-1]}")).json()
        assert stored["source_event_id"] == f"http-batch:{last_index}:{media}"
        retried = (await client.post(URL, content=body, headers=headers)).json()
        assert retried["duplicates"] == 2 and retried["event_ids"] == summary["event_ids"]


@pytest.mark.parametrize("media,body", [
    ("text/csv", b'id,note\n001,"never closed\n'),
    ("text/csv", b'id,id\n001,002\n'),
    ("application/xml", b"<event><value></event>"),
    ("application/xml", b"<!DOCTYPE event><event><value>ok</value></event>"),
    ("application/yaml", b"plain scalar"),
    ("application/yaml", b"42: non_string_key"),
])
async def test_bad_body_has_a_location_reason_and_fix(client, media, body):
    response = await client.post(URL, content=body, headers={**KEY, "Content-Type": media})
    assert response.status_code == 400, response.text
    summary = response.json()
    if summary.get("rejected"):
        error = summary["rejected"][0]
        assert error["reason"] and (error.get("line") or error["row"]) and error["fix_example"]
    else:
        assert summary["detail"] and summary["field"] and summary["fix_example"]


async def test_xml_attributes_text_and_repeated_values(client):
    response = await client.post(URL, headers={**KEY, "Content-Type": "text/xml"},
                                 content=b'<event unit="C"><tag>A</tag><tag>B</tag>More</event>')
    (event_id,) = response.json()["event_ids"]
    stored = (await client.get(f"/v1/events/{event_id}")).json()
    assert stored["raw"] == {"@unit": "C", "tag": [{"#text": "A"}, {"#text": "B"}], "#text": "More"}
    assert stored["fields"]["tag.1.#text"] == "B"
    assert stored["status"] == "needs_review"


async def test_bare_text_is_retained_and_parser_mismatches_are_per_line(client, monkeypatch):
    headers = {**KEY, "Content-Type": "text/plain"}
    response = await client.post(URL, content=b"first line\n\nsecond line\n", headers=headers)
    assert response.status_code == 202
    for event_id, value in zip(response.json()["event_ids"], ("first line", "second line"), strict=True):
        stored = (await client.get(f"/v1/events/{event_id}")).json()
        assert stored["raw"] == {"message": value} and stored["status"] == "needs_review"
    _text_parser(monkeypatch)
    parsed = await client.post(URL, content=b'label="normal line"\n\nmalformed-line\n', headers=headers)
    assert parsed.status_code == 202
    result = parsed.json()
    assert result["accepted"] == 1 and result["rejected"][0]["line"] == 3
    event = (await client.get(f"/v1/events/{result['event_ids'][0]}")).json()
    assert event["raw"] == {"label": "normal line"}


async def test_new_csv_columns_suggest_verify_confirm_and_route(live_http, inbox_pool, monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    columns = ("component_name", "signal", "clock_reading", "problem_code", "message")
    headers = {**KEY, "Content-Type": "text/csv"}
    first = await live_http.post(URL, headers=headers,
                                 content=_csv_text(columns, [("station", "ERR", AT, ALERT, "initial")]))
    (event_id,) = first.json()["event_ids"]
    event = (await live_http.get(f"/v1/events/{event_id}")).json()
    assert event["status"] == "needs_review"
    issues = (await live_http.get("/v1/schema-issues")).json()["items"]
    (issue,) = [issue for issue in issues if issue["sample_event_id"] == event_id]
    suggestion = await live_http.post("/v1/schemas/suggest", json={"fingerprint": issue["fingerprint"]})
    assert suggestion.json()["source"] == "heuristic"
    draft = suggestion.json()["draft"]
    assert draft["aliases"]["service"] == "component_name" and draft["aliases"]["severity"] == "signal"
    draft["aliases"]["time"] = "clock_reading"  # The reviewer confirms an unfamiliar timestamp name.
    assert draft["aliases"]["error_code"] == "problem_code"
    await _write_rule(live_http, draft["name"])
    verified = await live_http.post("/v1/schemas/verify", json={"schema": draft, "samples": [
        {"event": event["raw"], "expect": {"recipients": ["sam", "team:settlements"], "channels": ["sms", "ui"]}},
        {"event": dict(event["raw"], problem_code=QUIET), "expect": "quiet"},
    ]})
    assert verified.json()["all_pass"], verified.text
    published = await live_http.post("/v1/schemas", headers={"X-User": "ops"}, json=draft,
                                    params={"fingerprint": issue["fingerprint"]})
    assert published.status_code == 200 and published.json()["reprocessed"] == 1
    assert (await live_http.get(f"/v1/events/{event_id}")).json()["status"] == "pending"
    # Process the retained sample, then use a new service to distinguish the next alert group.
    try:
        assert await process_batch(inbox_pool) == 1
        next_response = await live_http.post(URL, headers=headers, content=_csv_text(
            columns, [("other-station", "ERR", AT, ALERT, "next row")]))
        (next_id,) = next_response.json()["event_ids"]
        assert await process_batch(inbox_pool) == 1
        assert (await live_http.get(f"/v1/events/{next_id}")).json()["schema_id"] == draft["id"]
        assert await _routes(inbox_pool, next_id) == ROUTES
    finally:
        await close_settings(inbox_pool)
