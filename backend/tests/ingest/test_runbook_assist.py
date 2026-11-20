from unittest.mock import AsyncMock

import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from alerting import llm
from alerting.api.routes_runbook_assist import router
from alerting.assist.runbook_structure import StepHint, StepHints, structure
from alerting.runbooks.render import render


async def test_offline_text_compiles_to_yi_steps(monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    call = AsyncMock()
    monkeypatch.setattr(llm, "structured", call)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/runbooks/structure", json={"text": """# Recovery
1. Inspect metrics for {{system.component}}.
2. Restart service {{system.component}}.
3. If latency persists, roll it back.
4. Retry workflow nightly-42.
5. Escalate to release-engineering.
6. Delete the broken cache."""})
        assert (await client.post("/v1/runbooks/structure", json={"text": "   "})).status_code == 422

    assert response.status_code == 200, response.text
    result = response.json()
    draft = result["draft"]
    assert result["source"] == "heuristic"
    assert draft["format"] == "yaml-steps"
    assert yaml.safe_load(draft["content"])["steps"] == draft["steps"]
    rendered = render({"id": "new-recovery", **draft}, {"system.component": "search"}, approvers=["lee"])
    assert [(s["action"], s["approval"]) for s in rendered["steps"]] == [
        (None, "none"), ("restart_service", "required"), (None, "required"),
        ("retry_workflow", "none"), ("escalate", "required"), (None, "required"),
    ]
    assert rendered["steps"][1]["params"] == {"service": "search"}
    assert rendered["steps"][1]["approvers"] == ["lee"]
    call.assert_not_awaited()


async def test_model_only_selects_grounded_allowed_actions_and_is_cached(monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    call = AsyncMock(return_value=StepHints(steps=[
        StepHint(index=1, action="restart_service", params=[{"name": "service", "value": "search"}]),
        StepHint(index=2, action="call_webhook", params=[{"name": "url", "value": "https://example.invalid/blocked"}]),
        StepHint(index=3, action="retry_workflow", params=[{"name": "workflowId", "value": "job-22"}]),
        StepHint(index=4, action="unregistered_action"),
        StepHint(index=5, action="restart_service", params=[{"name": "service", "value": "invented"}]),
    ]))
    monkeypatch.setattr(llm, "structured", call)
    text = """Bounce search; token=demo-secret
Call webhook https://example.invalid/blocked.
Never retry workflow job-22.
Inspect metrics. Notify somebody@example.invalid.
Restart the appropriate service."""
    result = await structure(text)
    assert result == await structure(text)
    assert result["source"] == "llm"
    step, *other_steps = result["draft"]["steps"]
    assert (step["action"], step["approval"], step["service"]) == ("restart_service", "required", "search")
    assert all("action" not in other for other in other_steps)
    call.assert_awaited_once()
    sent = call.await_args.kwargs["user"]
    assert len(sent.encode()) <= 8000
    for private in ("demo-secret", "somebody@example.invalid"):
        assert private not in sent and private not in result["draft"]["content"]


async def test_unavailable_model_keeps_unknown_and_conditional_steps_manual(monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "structured", AsyncMock(return_value=None))
    result = await structure("Check Grafana. If the lag continues, restart service alpha. Roll the release back.")
    assert result["source"] == "heuristic"
    assert len(result["draft"]["steps"]) == 3
    assert [s["approval"] for s in result["draft"]["steps"]] == ["none", "required", "required"]
    assert all("action" not in s for s in result["draft"]["steps"])
