"""Team maintenance windows and their compiled, read-only matchers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from alerting.contracts import Severity
from alerting.rules.dsl import Predicate, compile as compile_match


SILENCE_COLUMNS = 'id,team_id,matcher AS "match",include_critical,created_by,created_at,expires_at'


class SilenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: str = Field(min_length=1)
    match: dict[str, Any]
    expires_at: AwareDatetime
    include_critical: bool = False

    @field_validator("match")
    @classmethod
    def validate_match(cls, matcher):
        try:
            compile_match(matcher)
        except (TypeError, RecursionError) as exc:
            raise ValueError("Invalid silence matcher") from exc
        return matcher


@dataclass(frozen=True)
class Silence:
    id: int
    team_id: str
    created_at: datetime
    expires_at: datetime
    created_by: str
    include_critical: bool
    predicate: Predicate

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            team_id=row["team_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            created_by=row["created_by"],
            include_critical=row["include_critical"],
            predicate=compile_match(row["match"]),
        )


async def active_rows(conn, at: datetime, *, team: str | None = None) -> list[dict]:
    conditions = "revoked_at is null and expires_at > %s"
    params = [at]
    if team is not None:
        conditions += " and team_id = %s"
        params.append(team)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"select {SILENCE_COLUMNS} from silences where {conditions} order by expires_at,id",
            params,
        )
        return await cur.fetchall()


async def load_active(conn, at: datetime, *, team: str | None = None) -> tuple[Silence, ...]:
    return tuple(Silence.from_row(row) for row in await active_rows(conn, at, team=team))


def active_silence(silences, fields, at: datetime, *, severity: str | Severity) -> Silence | None:
    """Select the latest-ending matching silence from the rule team's snapshot."""
    candidates = (
        silence
        for silence in silences
        if silence.created_at <= at < silence.expires_at
        and (severity != Severity.critical or silence.include_critical)
        and silence.predicate(fields)[0]
    )
    return max(candidates, key=lambda silence: (silence.expires_at, -silence.id), default=None)
