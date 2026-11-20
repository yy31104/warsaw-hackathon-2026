import json
from copy import deepcopy

import pytest

from alerting.rules.engine import Ruleset
from alerting.rules.safety import verify
from alerting.rules.storage import Settings
from .test_golden import GOLD, ROOT, START, merge


def test_published_verify_unit_change_and_unscaled_counterexample():
    settings = Settings.load()
    case = next(c for c in GOLD["cases"] if c["id"] == "verify-unit-change")
    samples = []
    for example in case["body"]["samples"]:
        raw = json.loads((ROOT / example["event_file"]).read_text())
        merge(raw, example["mutate"])
        samples.append({"event": raw, "expect": example["expect"]})
    schema = case["body"]["schema"]
    before = deepcopy((schema, samples, settings.ruleset.schemas, settings.directory))
    checked = verify(schema, samples, settings.ruleset, settings.directory, settings.runbooks, START)
    assert checked["all_pass"] == case["expect"]["all_pass"]
    quiet, notified = checked["samples"]
    assert quiet["actual"] == {"channels": [], "recipients": []}
    assert notified["actual"] == samples[1]["expect"]
    assert all(
        set(case["expect"]["deciding_fields_include"]) <= set(s["deciding_fields"])
        for s in checked["samples"]
    )
    assert quiet["fields"]["payload.notional"] == 250_000
    assert quiet["fields"]["payload.notional.raw"] == 25_000_000
    assert not quiet["unit_warnings"]
    unscaled = verify(
        {"base": schema["base"]}, samples, settings.ruleset, settings.directory, [], START
    )
    assert not unscaled["all_pass"] and not unscaled["samples"][0]["passed"]
    assert case["without_transform_expect"]["unit_warning"] in unscaled["samples"][0]["unit_warnings"]
    assert (schema, samples, settings.ruleset.schemas, settings.directory) == before


def test_verify_uses_detector_transformed_aliases_and_deciding_branches():
    settings = Settings.load()
    draft = {
        "id": "signal", "name": "signal.{{values.0}}", "detect": {"kind": "reading"},
        "aliases": {"severity": "values.0", "time": "at"}, "time_format": "epoch_s",
        "severity_map": {"2": "critical"}, "transforms": {"values.0": {"scale": 0.1, "unit": "level"}},
    }
    rule = {
        "id": "high",
        "match": {"any": [
            {"all": [{"spare": "yes"}, {"impossible": 1}]},
            {"not": {"field": "$severity", "op": "lt", "value": "critical"}},
        ]},
        "route": [{"to": "user:anna", "channels": ["ui"]}],
    }
    ruleset = Ruleset([rule])
    reading = {"kind": "reading", "values": ["20"], "at": 60, "spare": "yes", "password": "demo"}
    checked = verify(draft, [
        {"event": reading, "expect": "notify"},
        {"event": {**reading, "kind": "different"}, "expect": "quiet"},
    ], ruleset, settings.directory, [], START)
    assert checked["all_pass"]
    notified, quiet = checked["samples"]
    assert notified["actual"] == {"recipients": ["anna"], "channels": ["ui"]}
    assert notified["deciding_fields"] == ["$severity"]
    assert notified["fields"]["values.0.raw"] == "20"
    assert notified["fields"]["$schema"] == "signal.2"
    assert notified["fields"]["$severity"] == "critical"
    assert notified["fields"]["$time"] == "1970-01-01T00:01:00+00:00"
    assert notified["fields"]["password"] == "<redacted>"
    assert quiet["deciding_fields"] == ["kind"]


@pytest.mark.parametrize("scale", ["NaN", "not-a-scale", True])
def test_verify_rejects_invalid_scales(scale):
    settings = Settings.load()
    with pytest.raises(ValueError, match="scale"):
        verify(
            {"id": "sample", "transforms": {"size": {"scale": scale}}},
            [{"event": {"size": 10}, "expect": "quiet"}],
            Ruleset([]), settings.directory, [], START,
        )


def test_samples_have_independent_state_and_exact_recipient_expectations():
    rule = {
        "id": "outlier", "match": {"field": "size", "op": "in", "value": [20, 21]},
        "route": [{"to": "team:rates", "channels": ["ui"]}],
    }
    examples = [
        {"event": {"size": 20}, "expect": {"channels": ["ui"]}},
        {"event": {"size": 20}, "expect": {"recipients": ["absent"]}},
    ]
    result = verify({"id": "reading"}, examples, Ruleset([rule]), Settings.load().directory, [], START)
    first, second = result["samples"]
    assert first["passed"] and not second["passed"] and not result["all_pass"]
    assert first["actual"] == second["actual"] == {"recipients": ["team:rates"], "channels": ["ui"]}
    assert first["unit_warnings"] == ["size"]
