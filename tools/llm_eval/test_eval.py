"""Regression checks for misattribution, safety scoring, and the cumulative spending limit."""
from copy import deepcopy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.llm_eval.ledger import Journal, Rate, usd
from tools.llm_eval.scoring import alias_score, hint_steps, step_score
from tools.llm_eval import runner


def test_extra_aliases_and_wrong_paths_cannot_earn_full_credit():
    got = {"service": "labels.name", "message": "note", "environment": "other"}
    truth = {"service": "actual.name", "message": "note"}
    scores = alias_score(truth, got)
    assert scores["fields"] == [1, 3]
    assert scores["unexpected_aliases"] == 1
    assert scores["exact_alias_sets"] == [0, 1]


def test_order_values_and_approval_are_independent():
    truth = [
        {"text": "Inspect charts.", "action": None, "params": {}, "approval": "none"},
        {"text": "Restart service amber.", "action": "restart_service",
         "params": {"service": "amber"}, "approval": "required"},
    ]
    got = [deepcopy(truth[1]), deepcopy(truth[0])]
    got[0]["params"] = {"service": "made-up", "flag": "invented"}
    got[0]["approval"] = "none"
    scores = step_score(truth, got)
    assert scores["steps"] == [0, 2]
    assert scores["invented_parameters"] == 2
    assert scores["risky_without_approval"] == 1
    assert scores["missing_risky_steps"] == 0
    assert step_score(truth, truth)["steps"] == [2, 2]
    assert step_score(truth, truth[:1])["missing_risky_steps"] == 1


def test_extra_unknown_and_repeated_provider_candidates():
    truth = [{"text": "Wait.", "action": None, "params": {}, "approval": "none"}]
    raw = {"steps": [
        {"index": 1, "action": "restart_service", "params": [{"name": "service", "value": "x"}]},
        {"index": 1, "action": "other", "params": []},
        {"index": 3, "action": "other", "params": []},
    ]}
    scores = hint_steps(truth, raw)
    assert scores["candidate_steps"] == [0, 3]
    assert scores["invented_parameters"] == 1
    assert scores["unsafe_candidates"] == 3


def rate():
    return Rate.from_dict({"input_per_million": "2", "cached_input_per_million": "1",
                           "output_per_million": "8", "max_context_tokens": 1_000_000})


def usage(i=20, c=5, o=10, outcome="success", model="snapshot"):
    return SimpleNamespace(provider="openai", model="snapshot", response_model=model, outcome=outcome,
                           input_tokens=i, cached_input_tokens=c, output_tokens=o, latency_ms=2.4)


def test_upfront_reservation_persists_and_incomplete_usage_is_never_released(tmp_path):
    path = tmp_path / "budget.sqlite3"
    j = Journal(path)
    j.reserve("baseline", "snapshot", "case-1", rate(), 1000)
    pending = j.exposure()
    assert usd(pending) == "2.008"
    j = Journal(path)
    assert j.exposure() == pending
    j.reserve("tuned", "snapshot", "case-2", rate(), 1000)
    j.settle("tuned", "snapshot", "case-2", rate(), usage(i=None, c=None, o=None, outcome="timeout"))
    with pytest.raises(ValueError, match="budget"):
        j.reserve("final", "snapshot", "case-3", rate(), 1000)
    assert j.exposure() == 2 * pending


def test_billed_invalid_counts_and_exact_served_model(tmp_path):
    j = Journal(tmp_path / "budget.sqlite3")
    j.reserve("baseline", "snapshot", "x", rate(), 100)
    detail = j.settle("baseline", "snapshot", "x", rate(), usage(outcome="invalid"))
    assert detail["estimated_cost_usd"] == "0.000115"
    assert usd(j.exposure()) == "0.000115"
    j.reserve("baseline", "snapshot", "y", rate(), 100)
    detail = j.settle("baseline", "snapshot", "y", rate(), usage(model="different-model"))
    assert detail["estimated_cost_usd"] is None
    assert detail["model_verified"] is False
    assert j.exposure() > 2_000_000_000


def test_preflight_missing_usage_stays_reserved_at_highest_rate_and_breach_stops_future_calls(tmp_path):
    j = Journal(tmp_path / "budget.sqlite3")
    r = Rate.from_dict(runner.RATE_CARD["gpt-6.2-sol"])
    j.reserve("baseline", "snapshot", "x", r, 1536, counted=250, count_ms=700.0)
    assert usd(j.exposure()) == "0.018545"
    before = j.exposure()
    receipt = j.settle("baseline", "snapshot", "x", r, usage(i=None, c=None, o=None, outcome="timeout", model=None))
    assert receipt["estimated_cost_usd"] is None and j.exposure() == before
    assert receipt["preflight_input_tokens"] == 250 and receipt["reserved_input_tokens"] == 1274
    j.reserve("baseline", "snapshot", "y", r, 1536, counted=250)
    j.settle("baseline", "snapshot", "y", r, usage(i=1400))
    with pytest.raises(ValueError, match="provider exceeded"):
        j.reserve("tuned", "snapshot", "z", r, 1536, counted=250)


def test_only_recognized_calendar_snapshot_and_cache_write_price(tmp_path):
    j = Journal(tmp_path / "budget.sqlite3")
    r = Rate.from_dict(runner.RATE_CARD["gpt-6-luna"])
    for case, response_model, verified in (("a", "snapshot-2026-11-21", True),
                                            ("b", "snapshot-2026-13-88", False),
                                            ("c", "snapshot-pro-2026-11-21", False)):
        j.reserve("baseline", "snapshot", case, r, 100, counted=200)
        obs = usage(i=100, c=20, o=50, model=response_model)
        obs.cache_write_input_tokens = 40
        detail = j.settle("baseline", "snapshot", case, r, obs)
        assert detail["model_verified"] == verified
        if verified:
            assert detail["estimated_cost_usd"] == "0.0000342"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout"])
async def test_product_and_provider_scores_and_preflight_latency(tmp_path, monkeypatch, outcome):
    from alerting import llm
    from alerting.assist.runbook_structure import StepHints

    state = {"now": 0.0}
    monkeypatch.setattr(runner, "perf_counter", lambda: state["now"])
    monkeypatch.setattr(llm, "status", lambda: {"enabled": True, "provider": "openai", "model": "snapshot"}, raising=False)
    monkeypatch.setattr(llm, "enabled", lambda: True)

    @contextmanager
    def observer(fn, *, max_output_tokens):
        assert max_output_tokens == runner.OUTPUT_CAP
        state["observe"] = fn
        yield

    async def counted(_, *, system, user):
        state["now"] += 1.50
        state["input"] = (system, user)
        return 300

    async def generate(_, *, system, user):
        assert state["input"] == (system, user)
        state["now"] += 0.2
        if outcome == "timeout":
            state["observe"](usage(i=None, c=None, o=None, outcome="timeout", model=None))
            return None
        state["observe"](usage())
        return StepHints(steps=[{"index": 1, "action": "restart_service", "params": [{"name": "service", "value": "amber"}]}])

    monkeypatch.setattr(llm, "observe_calls", observer, raising=False)
    monkeypatch.setattr(llm, "count_openai_input", counted, raising=False)
    monkeypatch.setattr(llm, "structured", generate)
    j = Journal(tmp_path / "budget.sqlite3")
    c = {"id": "fixture", "text": "Bounce amber.", "steps": [
        {"text": "Bounce amber.", "action": "restart_service", "params": {"service": "amber"}, "approval": "required"}]}
    row = await runner.evaluate("baseline", "snapshot", "runbook", c, j, rate())
    assert row["endpoint_latency_ms"] == pytest.approx(200)
    assert row["call"]["preflight_latency_ms"] == pytest.approx(1500)
    if outcome == "timeout":
        assert row["attribution"] == "provider_failure_fallback" and row["provider_only"] is None
        assert row["product"]["steps"] == [0, 1] and j.exposure() > 0
    else:
        assert row["attribution"] == "model_contributed" and row["product_source"] == "llm"
        assert row["provider_only"]["candidate_steps"] == row["product"]["steps"] == [1, 1]
