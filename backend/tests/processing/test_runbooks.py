from alerting.config import files
from alerting.runbooks.render import render

from .test_golden import CASES, case_event


def test_settlement_steps():
    book = files.runbooks()[0]
    steps = render(book, case_event(CASES[0]).fields)["steps"]
    assert [s["action"] for s in steps] == ["http_get", "retry_workflow", "escalate"]
    assert steps[1]["params"]["workflowId"] == "WF-20261023-00042"
    assert steps[2]["approval"] == "required"


def test_manual_formats():
    books = files.runbooks()
    markdown = render(books[1], {})
    assert len(markdown["steps"]) == 4 and markdown["steps"][1]["approval"] == "required"
    assert render(books[2], {})["can_structure"]
    assert (
        render({"id": "url", "format": "url", "content": "https://example.com"}, {})["steps"][0][
            "url"
        ]
        == "https://example.com"
    )
    unknown = render(
        {"id": "x", "format": "yaml-steps", "content": "steps: [{action: unknown}]"}, {}
    )
    assert unknown["steps"][0]["kind"] == "manual"


def test_allowlist():
    import pytest
    from alerting.runbooks.actions.network import local_path

    assert local_path("http://mock-ops/health") == "/health"
    for url in (
        "http://example.org/health",
        "https://mock-ops/health",
        "http://mock-ops:22/foo",
        "http://mock-ops@internal/probe",
    ):
        with pytest.raises(ValueError):
            local_path(url)
