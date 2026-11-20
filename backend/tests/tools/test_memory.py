import pytest
from pydantic import ValidationError

from alerting.memory import ResolutionNote, incident_key


def test_incident_key_uses_canonical_metadata_and_scalar_aliases():
    assert incident_key(
        {
            "service": None,
            "schema_id": "custom",
            "schema_version": 3,
            "schema_name": None,
            "fields": {
                "$service": " queue ",
                "$error_code": 503,
                "$schema": "queue.stalled",
                "private": "omit",
            },
        }
    ) == {
        "service": "queue",
        "error_code": "503",
        "schema_id": "custom",
        "schema": "queue.stalled",
        "schema_version": 3,
    }
    key = incident_key(
        {
            "schema_id": "legacy",
            "fields": {"$service": "<redacted>", "$error_code": {"private": "secret"}},
        }
    )
    assert key["service"] is None and key["error_code"] is None and key["schema"] == "legacy"


def test_note_is_human_text_and_redacted_before_storage():
    note = ResolutionNote(note="  Queue drained. Contact person@example.com, token=test-secret  ")
    assert note.note == "Queue drained. Contact <email>, token=<redacted>"
    for value in ("", "  \n", "bad\x00note", "a" * 4001):
        with pytest.raises(ValidationError):
            ResolutionNote(note=value)
