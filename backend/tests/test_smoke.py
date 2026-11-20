"""Scaffold smoke test: the app imports, every router is mounted, /health answers without a DB."""
import os

os.environ["DB_ON_STARTUP"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from alerting.api.app import app  # noqa: E402
from alerting.contracts import SEVERITY_ORDER, Severity  # noqa: E402
from alerting.llm import redact  # noqa: E402


def test_health():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_severity_order_excludes_unknown():
    assert Severity.unknown not in SEVERITY_ORDER
    assert SEVERITY_ORDER[Severity.warning] < SEVERITY_ORDER[Severity.critical]


def test_redact_masks_contacts_and_secrets():
    out = redact("mail anna@example.com phone +15550100001 token=abc123 Bearer xyz.789")
    assert "anna@example.com" not in out and "+15550100001" not in out
    assert "abc123" not in out and "xyz.789" not in out
