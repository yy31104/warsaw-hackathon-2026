from copy import deepcopy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from alerting.clock import FixedClock
from alerting.ingest.normalize import normalize
from alerting.rules import stability as module
from alerting.rules.stability import stability


CLOCK = FixedClock(datetime(2026, 1, 15, 12, tzinfo=UTC))
DRAFT = {"id": "signal", "name": "reading", "detect": {}, "severity_default": "warning"}


def axis(id, kind, path, *values, **extra):
    return {"id": id, "kind": kind, "path": path, **extra,
            "candidates": [{"value": value, "source": "declared"} for value in values]}


def config(match, *, group=None):
    rule = {
        "id": "notify-desk", "owner": "team:desk", "match": match,
        "route": [{"to": "user:operator", "channels": ["ui"]}],
    }
    if group:
        rule["group"] = {"by": group}
    return {
        "revision": 7, "schemas": [], "rules": [rule], "runbooks": [],
        "directory": {
            "teams": [{"id": "desk", "members": ["operator"]}], "fallback_team": "desk",
            "users": [{"id": "operator", "timezone": "UTC",
                       "contacts": {"email": "operator@example.test", "sms": "+15550100001"}}],
        },
    }


def test_only_price_unit_changes_routing_and_naive_timezone_needs_no_question():
    snapshot = config({"field": "price", "op": "gt", "value": 100})
    draft = {**DRAFT, "aliases": {"time": "stamp"}}
    samples = [{"event": {"price": 750, "stamp": "2026-01-15T09:15:00"}}]
    hypotheses = [
        axis("source-zone", "timezone", "stamp", "UTC", "Europe/Warsaw"),
        axis("price-unit", "scale", "price", 1, 0.01),
    ]
    inputs = deepcopy((samples, draft, hypotheses, snapshot))
    report = stability(samples, draft, hypotheses, snapshot, clock=CLOCK)
    assert report == stability(samples, draft, hypotheses, snapshot, clock=CLOCK)
    assert (samples, draft, hypotheses, snapshot) == inputs
    assert report["complete"] and report["decision_invariant"] is False and report["semantics_confirmed"] is False
    assert report["evaluated_combinations"] == 4
    assert report["relevant_unknowns"] == ["price-unit"]
    assert [question["id"] for question in report["ordered_questions"]] == ["price-unit"]
    assert report["worst_case_questions"] == 1
    assert report["question_tree"]["question_id"] == "price-unit"
    assert [(group["outcome"][0]["matched_rule_ids"], len(group["hypotheses"])) for group in report["outcome_groups"]] == [
        (["notify-desk"], 2), ([], 2),
    ]
    assert report["snapshot"]["revision"] == 7


def test_exact_minimax_uses_different_followups_for_each_answer():
    snapshot = config({"any": [{"gate": 0, "left": 1}, {"gate": 1, "right": 1}]})
    hypotheses = [axis(p, "scale", p, 0, 1) for p in ("left", "right", "gate")]
    result = stability([{"event": {"gate": 1, "left": 1, "right": 1}}], DRAFT, hypotheses, snapshot, clock=CLOCK)
    tree = result["question_tree"]
    assert tree["question_id"] == "gate" and result["worst_case_questions"] == 2
    assert [branch["next"]["question_id"] for branch in tree["branches"]] == ["left", "right"]
    for branch in tree["branches"]:
        assert all("outcome_group" in leaf["next"] for leaf in branch["next"]["branches"])


def test_two_relevant_dimensions_need_two_questions_instead_of_three_in_declared_order():
    hypotheses = [axis(path, "scale", path, 0, 1) for path in ("irrelevant", "cost", "load")]
    report = stability([{"event": {"irrelevant": 1, "cost": 1, "load": 1}}], DRAFT, hypotheses,
                       config({"cost": 1, "load": 1}), clock=CLOCK)
    assert report["worst_case_questions"] == 2 and report["relevant_unknowns"] == ["cost", "load"]
    assert report["question_tree"]["branches"][0]["next"] == {"outcome_group": "g0"}


def test_distinct_alert_keys_remain_separate_when_every_delivery_is_identical():
    samples = [{"event": {"primary": "east", "secondary": "west"}}]
    result = stability(samples, DRAFT, [axis("service-source", "alias", "service", "primary", "secondary")],
                       config({}, group=["$service"]), clock=CLOCK)
    outcomes = [group["outcome"][0] for group in result["outcome_groups"]]
    assert {outcome["alerts"][0]["alert_key"] for outcome in outcomes} == {"east", "west"}
    assert all(outcome["deliveries"] == [{"recipient": "operator", "channel": "ui"}] for outcome in outcomes)
    assert not result["decision_invariant"]


def test_unresolved_critical_branch_keeps_in_review_but_unmatched_rank_cannot_page():
    draft = {**DRAFT, "aliases": {"severity": "rank"}}
    labels = [axis("rank-meaning", "severity", "rank", "warning", "critical", label="X1")]
    samples = [{"event": {"rank": "X1"}}]
    result = stability(samples, draft, labels, config({"$severity": "critical"}), clock=CLOCK)
    assert result["critical_possible"] is True and "may page critical: keep in review" in result["warnings"]
    notified = next(group["outcome"][0] for group in result["outcome_groups"] if group["outcome"][0]["deliveries"])
    assert notified["alerts"][0]["priority"] == "P0"
    quiet = stability(samples, draft, labels, config({"not_present": True}), clock=CLOCK)
    assert quiet["decision_invariant"] is True and quiet["critical_possible"] is False
    assert quiet["question_tree"] == {"outcome_group": "g0"} and quiet["worst_case_questions"] == 0


def test_clock_interpretation_preserves_raw_group_and_real_recipient_channel_pairs():
    snapshot = config({}, group=["stamp"])
    rule = snapshot["rules"][0]
    rule["route"] = [{"to": "user:operator", "channels": "preferences"}]
    snapshot["directory"]["users"][0]["preferences"] = [
        {"when": "08:00-16:00", "channels": ["email"]}, {"channels": ["sms"]},
    ]
    stamp = "2026-01-15T08:15:00"
    report = stability([{"event": {"stamp": stamp}}], {**DRAFT, "aliases": {"time": "stamp"}},
                       [axis("zone", "timezone", "stamp", "UTC", "Europe/Warsaw")], snapshot, clock=CLOCK)
    outcomes = [group["outcome"][0] for group in report["outcome_groups"]]
    assert [row["deliveries"][0]["channel"] for row in outcomes] == ["email", "sms"]
    assert [row["alerts"][0]["alert_key"] for row in outcomes] == [stamp, stamp]


def test_swapped_recipient_channel_pairs_are_distinct_even_when_both_marginal_sets_are_equal():
    snapshot = config({})
    snapshot["rules"][0]["route"] = [
        {"to": "user:operator", "channels": "preferences"}, {"to": "user:backup", "channels": "preferences"},
    ]
    operator = snapshot["directory"]["users"][0]
    backup = {**operator, "id": "backup"}
    snapshot["directory"]["users"].append(backup)
    operator["preferences"] = [{"when": "08:00-16:00", "channels": ["email"]}, {"channels": ["sms"]}]
    backup["preferences"] = [{"when": "08:00-16:00", "channels": ["sms"]}, {"channels": ["email"]}]
    result = stability([{"event": {"stamp": "2026-01-15T08:15:00"}}], {**DRAFT, "aliases": {"time": "stamp"}},
                       [axis("zone", "timezone", "stamp", "UTC", "Europe/Warsaw")], snapshot, clock=CLOCK)
    routes = [g["outcome"][0]["deliveries"] for g in result["outcome_groups"]]
    assert routes == [
        [{"recipient": "backup", "channel": "sms"}, {"recipient": "operator", "channel": "email"}],
        [{"recipient": "backup", "channel": "email"}, {"recipient": "operator", "channel": "sms"}],
    ]


def test_only_selected_source_alias_applies_its_rank_mapping():
    samples = [{"event": {"a": "X", "b": "X"}}]
    sources = axis("source", "alias", "severity", "a", "b")
    a = axis("a-rank", "severity", "a", "critical", label="X")
    b = axis("b-rank", "severity", "b", "warning", label="X")
    report = stability(samples, DRAFT, [a, sources, b], config({}), clock=CLOCK)
    assert [g["outcome"][0]["alerts"][0]["severity"] for g in report["outcome_groups"]] == ["critical", "warning"]
    assert report["relevant_unknowns"] == ["source"]


def test_hypothetical_timezone_preserves_producer_event_and_episode_identity():
    source = {"id": "desk", "transport": "http"}
    schema = {**DRAFT, "aliases": {"time": "stamp"}, "event_id": "{{$time}}", "episode_key": "{{$time}}"}
    raw = {"stamp": "2026-01-15T08:15:00"}
    baseline = normalize(raw, source, [schema], received_at=CLOCK.now())
    variant = normalize(raw, source, [schema], received_at=CLOCK.now(), naive_timezone=ZoneInfo("Europe/Warsaw"))
    assert baseline.occurred_at != variant.occurred_at
    assert baseline.raw == variant.raw == raw and baseline.source_event_id == variant.source_event_id
    assert baseline.fields["$episode"] == variant.fields["$episode"]


def test_sixty_five_combinations_is_incomplete_without_an_invariance_claim():
    report = stability([{"event": {"amount": 1}}], DRAFT, [axis("unit", "scale", "amount", *range(1, 66))],
                       config({}), clock=CLOCK)
    assert report["status"] == "INCOMPLETE" and report["complete"] is False
    assert report["decision_invariant"] is None and report["critical_possible"] is None
    assert report["total_combinations"] == 65 and report["evaluated_combinations"] == 0
    assert report["question_tree"] is None and report["outcome_groups"] == []


def test_sixty_four_worlds_are_evaluated_but_an_expired_budget_cannot_claim_invariance():
    samples, snapshot = [{"event": {"amount": 10}}], config({})
    domain = [axis("unit", "scale", "amount", *range(1, 65))]
    complete = stability(samples, DRAFT, domain, snapshot, clock=CLOCK)
    assert complete["evaluated_combinations"] == 64 and complete["decision_invariant"] is True
    expired = stability(samples, DRAFT, domain, snapshot, clock=CLOCK, deadline=0)
    assert expired["status"] == "INCOMPLETE" and expired["reason"] == "time_limit"
    assert expired["evaluated_combinations"] == 0 and expired["decision_invariant"] is None
    assert expired["critical_possible"] is None


def test_timeout_preserves_witnessed_critical_route_without_claiming_stability(monkeypatch):
    actual_verify, elapsed = module.verify, [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])

    def delayed_world(*args, **kwargs):
        report = actual_verify(*args, **kwargs)
        elapsed[0] = module.TIME_LIMIT_SECONDS + 1
        return report

    monkeypatch.setattr(module, "verify", delayed_world)
    hypotheses = [axis("rank", "severity", "label", "critical", "warning", label="X")]
    report = stability([{"event": {"label": "X"}}], DRAFT, hypotheses, config({}), clock=CLOCK)
    assert report["status"] == "INCOMPLETE" and report["evaluated_combinations"] == 1
    assert report["critical_possible"] is True and report["warnings"] == [module.CRITICAL_WARNING]
    assert report["decision_invariant"] is None and report["question_tree"] is None
