"""Human-confirmed incident resolutions and their canonical matching fields."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alerting.llm import MAX_INPUT_CHARS, redact


class ResolutionNote(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note: str = Field(min_length=1, max_length=4000)
    verified_resolution: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("note", "verified_resolution")
    @classmethod
    def redact_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("Resolution text contains a NUL character")
        return redact(value)


def _label(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        return None
    label = str(value).strip()
    if not label or len(label) > MAX_INPUT_CHARS:
        return None
    # Two masked values or two missing template substitutions are never evidence of a match.
    markers = ("<email>", "<redacted>", "<token>", "<phone-or-number>", "<card-like>", "‹missing:")
    if any(marker in label for marker in markers) or redact(label) != label:
        return None
    return label


def incident_key(event: dict) -> dict:
    fields = event.get("fields") or {}
    schema_id = _label(event.get("schema_id"))
    return {
        "service": _label(event.get("service") or fields.get("$service")),
        "error_code": _label(fields.get("$error_code")),
        "schema_id": schema_id,
        "schema": _label(event.get("schema_name") or fields.get("$schema")) or schema_id,
        "schema_version": event.get("schema_version"),
    }
