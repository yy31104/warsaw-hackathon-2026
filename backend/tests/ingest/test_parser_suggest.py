from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from alerting import db, llm
from alerting.api.app import app
from alerting.config import loader
from alerting.ingest.normalize import parse_log_line


ENDPOINT = "/v1/sources/tray-reader/parser/suggest"

PRICING_DRAFT = {
    "pattern": r"^(?P<stamp>\S+)\s+(?P<level>[A-Z]+)\s+(?P<reader>\S+)\s+(?P<pairs>.*)$",
    "field_names": ["stamp", "level", "reader", "pairs"],
    "kv_group": "pairs",
}
PRICING_LINES = [
    '2026-11-20T10:42:01Z INFO reader-north tray="north wing" token="demo-secret-north demo-secret-tail"',
    '2026-11-20T10:42:02Z WARN reader-west tray="west wing" contact=qa@example.invalid',
    '2026-11-20T10:42:03Z ERROR reader-east tray="east wing" phone=+12025550123',
]


@pytest.fixture
async def parser_client(monkeypatch):
    sources = [
        {"id": "tray-reader", "transport": "logfile", "path": "/logs/trays.log", "version": 7,
         "origin": "file", "parser": {"type": "regex", "pattern": r"^(?P<legacy>legacy)$"}},
        {"id": "tray-http", "transport": "http"},
        {"id": "tray-queue", "transport": "rabbitmq"},
    ]

    @asynccontextmanager
    async def connection():
        yield object()

    registry = SimpleNamespace(sources=sources)
    monkeypatch.setattr(loader, "read_snapshot", AsyncMock(return_value=registry))
    # GET /sources uses loader.sources, which may import files in a real database.
    monkeypatch.setattr(loader, "sources", AsyncMock(return_value=sources))
    monkeypatch.setitem(app.dependency_overrides, db.pool, lambda: SimpleNamespace(connection=connection))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def provider(monkeypatch, request):
    monkeypatch.setenv("ASSIST_ENABLED", "1")
    monkeypatch.setenv("ASSIST_PROVIDER", "openai")
    monkeypatch.setenv("ASSIST_MODEL", request.node.name)
    monkeypatch.setenv("OPENAI_API_KEY", "test-double")
    call = AsyncMock(return_value=None)
    monkeypatch.setattr(llm, "structured", call)
    return call


def propose(provider, draft):
    provider.side_effect = lambda output_model, **kwargs: output_model(**draft)


def pasted_parser(result):
    document = "sources:\n  - id: tray-reader\n    transport: logfile\n" + result["parser_yaml"]
    source, = yaml.safe_load(document)["sources"]
    return source["parser"]


async def test_pricing_draft_parses_quoted_fields_and_leaves_source_unchanged(parser_client, provider):
    propose(provider, PRICING_DRAFT)
    before = (await parser_client.get("/v1/sources")).json()
    response = await parser_client.post(ENDPOINT, json={"lines": PRICING_LINES})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source"] == "llm" and result["model"] == llm.selection()[1]
    assert (result["matched"], result["total"], result["unmatched_examples"]) == (3, 3, [])
    draft = result["draft"]
    assert draft["field_names"] == PRICING_DRAFT["field_names"] and draft["kv_group"] == "pairs"
    parser = pasted_parser(result)
    assert parser == {"type": "regex", "pattern": draft["pattern"], "kv_group": "pairs"}
    records = [parse_log_line(line, parser) for line in PRICING_LINES]
    assert [record["tray"] for record in records] == ["north wing", "west wing", "east wing"]
    assert result["preview"] == [{"line_number": i, "fields": record} for i, record in enumerate(records, 1)]
    assert loader.sources.await_count == 1  # The preceding GET alone ran the file-importing lookup.
    assert (await parser_client.get("/v1/sources")).json() == before
    provider.assert_awaited_once()
    prompt = provider.await_args.kwargs["system"] + "\n" + provider.await_args.kwargs["user"]
    for private in ("demo-secret-north", "demo-secret-tail", "qa@example.invalid", "+12025550123"):
        assert private not in prompt


@pytest.mark.parametrize(("good", "expected_status"), [(4, 200), (3, 422)])
async def test_coverage_uses_every_submitted_line_and_reports_unmatched(parser_client, provider, good, expected_status):
    propose(provider, {"pattern": r"^reading (?P<station>\S+)$", "field_names": ["station"], "kv_group": None})
    stations = ["pad-amber", "pad-green", "pad-pink", "pad-violet"][:good]
    lines = [f"reading {station}" for station in stations]
    lines.insert(1, "unrecognized row token=demo-rejected")
    response = await parser_client.post(ENDPOINT, json={"lines": lines})
    assert response.status_code == expected_status, response.text
    report = response.json() if expected_status == 200 else response.json()["detail"]
    assert (report["matched"], report["total"]) == (good, len(lines))
    assert "unrecognized row" in str(report["unmatched_examples"])
    assert "demo-rejected" not in str(report["unmatched_examples"])
    if expected_status == 422:
        assert report["code"] and report["message"] and report["source"] == "llm"
    else:
        assert report["draft"].get("kv_group") is None
        assert report["preview"] == [
            {"line_number": index, "fields": {"station": station}}
            for index, station in zip([1, 3, 4, 5], stations, strict=True)
        ]
    provider.assert_awaited_once()
    prompt = provider.await_args.kwargs["user"]
    assert sum(marker in prompt for marker in [*stations, "unrecognized row"]) <= 3


async def test_invalid_model_regex_returns_a_reason_without_substituting_a_heuristic(parser_client, provider):
    propose(provider, {"pattern": "(?P<broken>", "field_names": ["broken"], "kv_group": None})
    response = await parser_client.post(ENDPOINT, json={"lines": PRICING_LINES})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["source"] == "llm" and detail["code"] and detail["message"]
    provider.assert_awaited_once()


async def test_unavailable_provider_returns_a_working_heuristic(parser_client, provider):
    response = await parser_client.post(ENDPOINT, json={"lines": PRICING_LINES[:1]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["source"], result["matched"], result["total"]) == ("heuristic", 1, 1)
    assert result["draft"]["field_names"]
    assert parse_log_line(PRICING_LINES[0], result["draft"])["tray"] == "north wing"
    provider.assert_awaited_once()


async def test_no_key_returns_a_delimited_parser_without_calling_the_provider(parser_client, provider, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    lines = [f"tray-{i}|steady reading|{i}" for i in range(20)]
    before = (await parser_client.get("/v1/sources")).json()
    response = await parser_client.post(ENDPOINT, json={"lines": lines})
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["source"], result["matched"], result["total"]) == ("heuristic", 20, 20)
    draft = result["draft"]
    assert draft.get("kv_group") is None
    parser = pasted_parser(result)
    assert parser == {"type": "regex", "pattern": draft["pattern"]}
    assert result["preview"] == [
        {"line_number": index, "fields": parse_log_line(line, parser)} for index, line in enumerate(lines, 1)
    ]
    fields = parse_log_line("new-tray|reading changed|42", parser)
    assert [fields[name] for name in draft["field_names"]] == ["new-tray", "reading changed", "42"]
    assert (await parser_client.get("/v1/sources")).json() == before
    provider.assert_not_awaited()


@pytest.mark.parametrize(("source", "status"), [("tray-http", 422), ("tray-queue", 422), ("missing-source", 404)])
async def test_only_registered_log_sources_can_request_a_suggestion(parser_client, provider, source, status):
    response = await parser_client.post(f"/v1/sources/{source}/parser/suggest", json={"lines": PRICING_LINES})
    assert response.status_code == status, response.text
    assert "source" in response.json()["detail"].lower()
    provider.assert_not_awaited()


@pytest.mark.parametrize("count", [0, 21])
async def test_rejects_sample_counts_outside_the_public_range(parser_client, provider, count):
    response = await parser_client.post(ENDPOINT, json={"lines": [PRICING_LINES[0]] * count})
    assert response.status_code == 422, response.text
    provider.assert_not_awaited()


async def test_oversized_physical_sample_is_rejected_before_model_call(parser_client, provider):
    # Include the line terminator in the byte budget, just as in live logfile ingest.
    result = await parser_client.post(ENDPOINT, json={"lines": ["x" * 16384 + "\n"]})
    assert result.status_code == 422
    assert "line too long for the configured parser" in result.text
    provider.assert_not_awaited()
