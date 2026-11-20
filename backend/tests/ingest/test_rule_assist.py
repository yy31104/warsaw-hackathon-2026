from unittest.mock import AsyncMock

from alerting import llm
from alerting.assist.rule_draft import ConditionHint, RouteHint, RuleHints, draft_rule
from alerting.rules.dsl import compile


DIRECTORY = {
    "users": [{"id": "jordan", "name": "Jordan Taylor"}, {"id": "reviewer"}],
    "teams": [{"id": "inventory", "name": "Inventory Team", "members": ["jordan"]}],
    "fallback_team": "inventory",
}


async def test_heuristic_generates_arbitrary_typed_conditions_and_resolves_named_target(monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    call = AsyncMock()
    monkeypatch.setattr(llm, "structured", call)
    result = await draft_rule(
        'Notify Jordan Taylor via SMS and UI when shipment.packageCount >= 17 and depot = "north and east" or severity is critical',
        DIRECTORY,
        actor="reviewer",
    )
    assert result["source"] == "heuristic"
    draft = result["draft"]
    assert draft["enabled"] is True
    assert draft["owner"] == "user:reviewer"
    assert draft["route"] == [{"to": "user:jordan", "channels": ["sms", "ui"]}]
    compare = compile(draft["match"])
    assert compare({"shipment.packageCount": 17, "depot": "north and east"})[0]
    assert compare({"$severity": "critical"})[0]
    assert not compare({"shipment.packageCount": 16, "depot": "north and east"})[0]
    call.assert_not_awaited()


async def test_heuristic_refuses_to_guess_an_unknown_predicate_or_recipient(monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    result = await draft_rule("Notify nobody-known when the total looks suspicious", DIRECTORY)
    assert result["source"] == "heuristic" and result["warnings"]
    assert not result["draft"]["enabled"]
    assert not result["draft"]["route"]
    assert not compile(result["draft"]["match"])({})[0]


async def test_common_natural_phrasing_uses_aliases_and_configured_team_id(monkeypatch):
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    result = await draft_rule("Alert the inventory on-call via UI for critical events from scanner-api", DIRECTORY)
    assert result["draft"]["route"] == [{"to": "oncall:inventory", "channels": ["ui"]}]
    compare = compile(result["draft"]["match"])
    assert compare({"$severity": "critical", "$service": "scanner-api"})[0]
    assert not compare({"$severity": "critical", "$service": "inventory-api"})[0]


async def test_structured_model_result_is_redacted_cached_and_uses_existing_recipients(monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    call = AsyncMock(return_value=RuleHints(
        clauses=[[ConditionHint(field="shelf.count", op="lte", value=4)]],
        routes=[RouteHint(to="oncall:inventory", channels=["ui"]),
                RouteHint(to="user:made-up", channels=["phone"])],
    ))
    monkeypatch.setattr(llm, "structured", call)
    text = "When a shelf is nearly empty, page inventory on-call (password=mock-password; jordan@example.invalid)."
    result = await draft_rule(text, DIRECTORY)
    assert result == await draft_rule(text, DIRECTORY)
    assert result["source"] == "llm"
    # An unrecognised route is surfaced for human review and never returned as a destination.
    assert result["warnings"] and result["draft"]["route"] == [{"to": "oncall:inventory", "channels": ["ui"]}]
    assert compile(result["draft"]["match"])({"shelf.count": 4})[0]
    call.assert_awaited_once()
    sent = call.await_args.kwargs["user"]
    assert len(sent.encode()) <= 8000 and "mock-password" not in sent and "jordan@example.invalid" not in sent
