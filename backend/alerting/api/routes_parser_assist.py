"""Preview a source parser without changing the source configuration."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from alerting.assist.parser_suggest import ParserDraft, check_drafts, model_draft
from alerting.config import loader
from alerting.db import pool
from alerting.ingest.normalize import MAX_BYTES
from alerting.ingest.regex_safety import check_line_size

router = APIRouter(tags=["sources"])


class ParserSamples(BaseModel):
    lines: list[str] = Field(min_length=1, max_length=20)

    @field_validator("lines")
    @classmethod
    def physical_records(cls, lines: list[str]) -> list[str]:
        records = []
        for line in lines:
            if len(line.encode("utf-8")) > MAX_BYTES:
                raise ValueError("each sample line must be at most 256 KiB")
            check_line_size(line)
            record = line.removesuffix("\n").removesuffix("\r")
            if "\n" in record or "\r" in record or "\0" in record:
                raise ValueError("provide one physical log line per array entry")
            records.append(record)
        return records


class ParsedLine(BaseModel):
    line_number: int
    fields: dict[str, str]


class ParserSuggestion(BaseModel):
    draft: ParserDraft
    matched: int
    total: int
    unmatched_examples: list[str]
    preview: list[ParsedLine]
    parser_yaml: str
    source: Literal["llm", "heuristic"]
    model: str | None = None


@router.post("/sources/{id}/parser/suggest", response_model=ParserSuggestion, response_model_exclude_none=True)
async def suggest_parser(id: str, body: ParserSamples, response: Response, db=Depends(pool)):
    """Preview named captures and expanded logfmt fields, numbered from the first input line (1).

    A draft must parse at least 80% of the submitted records. Paste its YAML under the
    matching source in config/sources.yaml to apply it to future lines.
    """
    async with db.connection() as conn:
        sources = (await loader.read_snapshot(conn)).sources
    source = next((s for s in sources if s["id"] == id), None)
    if source is None:
        raise HTTPException(404, "Unknown source")
    if source["transport"] != "logfile":
        raise HTTPException(422, "Parser suggestions require a logfile source")

    response.headers["Cache-Control"] = "no-store"
    proposed, model = await model_draft(body.lines)
    result = await run_in_threadpool(check_drafts, body.lines, proposed, model)
    if result["draft"] is None:
        raise HTTPException(422, detail=result, headers={"Cache-Control": "no-store"})
    return result
