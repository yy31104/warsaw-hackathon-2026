import pytest

from alerting.ingest.inputs import prepare


@pytest.mark.parametrize(("demo", "header", "expected"), [
    ("1", "raise", "raise"), ("0", "raise", None), ("1", "other", None), ("1", None, None),
])
def test_fault_injection_requires_demo_mode_and_explicit_header(monkeypatch, demo, header, expected):
    monkeypatch.setenv("DEMO_MODE", demo)
    # The producer's event body is data. The golden runner sets the separate demo transport header.
    result = prepare({"trace": {"demo_fault": "raise"}}, {"id": "bus", "transport": "rabbitmq"}, [],
                     headers={"x-demo-fault": header})
    assert result.trace.get("demo_fault") == expected
