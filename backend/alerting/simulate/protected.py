"""Finite delivery protections for an in-memory rule optimization preview."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from alerting.contracts import CanonicalEvent, Severity
from alerting.routing.directory import by_id, target_people, team_for
from alerting.rules import safety
from alerting.rules.dsl import compile as compile_match
from alerting.rules.engine import Ruleset, duration
from alerting.simulate.history import replay_history


_MAX_DELAY = timedelta(seconds=60)
_APP_DIR = Path(__file__).resolve().parents[2]


def _labelled_references(rule_id):
    fixture_dir = _APP_DIR / "fixtures" / "protected"
    if not fixture_dir.is_dir():
        fixture_dir = _APP_DIR.parent / "fixtures" / "protected"
    for path in sorted(fixture_dir.glob("*.yaml")):
        fixture = yaml.safe_load(path.read_text(encoding="utf-8"))
        if fixture["rule_id"] == rule_id:
            yield from fixture["episodes"]


def _probe_event(fields, at, schema_id=None):
    source, transport = fields.get("$source"), fields.get("$transport")
    return CanonicalEvent(
        id=1, source_id=source if isinstance(source, str) else "optimizer-preview",
        transport=transport if transport in ("http", "rabbitmq", "logfile") else "http", schema_id=schema_id,
        payload_hash="", raw={}, fields=fields, severity=fields["$severity"], received_at=at,
    )


def _scheduled_wait(rule):
    if "expect" in rule:
        return duration(rule["expect"]["every"])
    group = rule.get("group", {})
    return duration(group.get("every", "5m")) if group.get("mode") == "digest" else timedelta()


def _first_times(replay, event_id):
    first = {}
    for recipient, channel, at in replay.planned_deliveries.get(event_id, ()):
        pair = (recipient, channel)
        first[pair] = min(first.get(pair, at), at)
    return first


def _loss(before, after, **identity):
    for (recipient, channel), original in sorted(before.items()):
        proposed = after.get((recipient, channel))
        if proposed is None:
            return {**identity, "recipient": recipient, "channel": channel, "kind": "missing"}
        if proposed - original > _MAX_DELAY:
            return {
                **identity, "recipient": recipient, "channel": channel, "kind": "delayed",
                "delay_s": (proposed - original).total_seconds(),
            }
    return None


def _first_change(before, after, **identity):
    for recipient, channel in sorted(before.keys() | after.keys()):
        pair = recipient, channel
        original, proposed = before.get(pair), after.get(pair)
        if original == proposed:
            continue
        change = {**identity, "recipient": recipient, "channel": channel}
        if original is None:
            return {**change, "kind": "added"}
        if proposed is None:
            return {**change, "kind": "missing"}
        delay = (proposed - original).total_seconds()
        return {**change, "kind": "delayed" if delay > 0 else "earlier", "delay_s": delay}
    return None


def _requirements(rule, directory):
    users, teams = by_id(directory["users"]), by_id(directory["teams"])
    team_id = team_for(rule.get("owner", ""), directory)
    team_channels = set(teams.get(team_id, {}).get("required_channels", {}).get("critical", ()))
    requirements, route_people, shared_ui = [], set(), set()
    for route in rule.get("route", ()):
        target = route["to"]
        people = set(target_people(target, teams, users)) & users.keys()
        route_people.update(people)
        team_target = target.startswith("team:") and target[5:] in teams
        if team_target:
            shared_ui.add(target)
        channels = route.get("channels", "preferences")
        if channels != "preferences":
            for channel in channels:
                recipients = {target} if channel == "ui" and team_target else people
                requirements.append(("explicit channels", target, channel, recipients))
    for channel in sorted(team_channels):
        recipients = route_people | shared_ui if channel == "ui" else route_people
        requirements.append(("team required_channels", f"team:{team_id}", channel, recipients))
    return requirements, team_channels


def _reachable(recipients, channel, users):
    return [
        recipient for recipient in recipients
        if channel == "ui" or users.get(recipient, {}).get("contacts", {}).get(channel)
    ]


def _boundary_context(scenario, episode_id):
    return {"episode_id": episode_id, "scenario": {
        key: scenario[key] for key in ("timezone", "local_time", "missing_contact")
    }}


@dataclass(frozen=True)
class _Probe:
    event: CanonicalEvent
    directory: dict
    context: dict
    pairs: dict
    critical_pairs: dict


class ProtectedScenarios:
    """Prepare baseline probes once and check each proposed draft without writing state."""

    def __init__(self, rule, schemas, directory, runbooks, acted_on=(), references=None):
        self.schemas = list(schemas)
        self.directory = directory
        self.runbooks = runbooks
        self.rule = rule
        self.acted_on = sorted((row["alert_id"], row["event_id"]) for row in acted_on)
        self._requirements, self._team_channels = _requirements(rule, directory)
        self._declared_pairs = {
            (recipient, channel) for kind, _, channel, recipients in self._requirements
            if kind == "explicit channels" for recipient in recipients
        }
        self._limitations = {}
        wait = _scheduled_wait(rule)
        labels = _labelled_references(rule["id"]) if references is None else references
        self._labels = [self._reference(label, rule, directory, wait) for label in labels]
        self._boundaries = self._safety_probes(rule, directory, wait)

    def _plan(self, event, rule, directory, wait):
        replay = replay_history(
            (event,), rule, self.schemas, directory, self.runbooks, through=event.received_at + wait,
        )
        return _first_times(replay, event.id), event.id in replay.critical_events

    def _reference(self, label, rule, directory, wait):
        at = datetime.fromisoformat(str(label["at"]))
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        at = at.astimezone(UTC)
        event = _probe_event(label["fields"], at, label.get("schema_id"))
        plans, _ = self._plan(event, rule, directory, wait)
        context = {"episode_id": label["episode_id"], "note": label["note"]}
        pairs = {}
        for expected in label["protect"]:
            pair = expected["recipient"], expected["channel"]
            pairs[pair] = plans.get(pair, at + wait)
            if pair not in plans:
                self._limitations[("labelled_reference", label["episode_id"], pair)] = {
                    **context, "source": "labelled_reference", **expected,
                    "reason": "The baseline has no planned delivery for this labelled destination.",
                }
        return _Probe(event, directory, context, pairs, {})

    def _required_baseline(self, plans):
        return {
            pair: at for pair, at in plans.items()
            if pair in self._declared_pairs or pair[1] in self._team_channels
        }

    def _critical_probe_pairs(self, plans, event, directory, wait, context):
        pairs = self._required_baseline(plans)
        users = by_id(directory["users"])
        absent = context["scenario"]["missing_contact"]
        for kind, target, channel, recipients in self._requirements:
            if any(delivery_channel == channel and (kind == "team required_channels" or recipient in recipients)
                   for recipient, delivery_channel in plans):
                continue
            # Missing-contact probes deliberately remove a destination. Record an inherited
            # limitation there, while retaining every pair the baseline can still reach.
            self._limitations.setdefault((kind, target, channel, absent), {
                **context, "source": "critical_channels", "requirement": kind,
                "target": target, "channel": channel,
                "reason": "The baseline has no planned delivery for this required critical channel.",
            })
            reachable = _reachable(recipients, channel, users)
            if absent is None and len(reachable) == 1:
                pairs[(reachable[0], channel)] = event.received_at + wait
        return pairs

    def _safety_probes(self, rule, directory, wait):
        report = safety.check(rule, Ruleset([rule], self.schemas), self.schemas, directory)
        for warning in report["warnings"]:
            if warning["problem"] != "never fires":
                self._limitations[("safety_boundary", warning.get("field"))] = {
                    "source": "safety_boundary", "reason": warning["problem"], **warning,
                }
        directories, probes = {None: directory}, []
        for scenario in report["scenarios_checked"]:
            if not scenario["matched"]:
                continue
            absent = scenario["missing_contact"]
            if absent not in directories:
                directories[absent] = {**directory, "users": [
                    {**user, "contacts": {}} if user["id"] == absent else user
                    for user in directory["users"]
                ]}
            at = datetime.fromisoformat(scenario["local_time"]).astimezone(UTC)
            event = _probe_event(scenario["fields"], at)
            plans, critical = self._plan(event, rule, directories[absent], wait)
            context = _boundary_context(scenario, f"safety-boundary-{len(probes) + 1}")
            required = (
                self._critical_probe_pairs(plans, event, directories[absent], wait, context) if critical else {}
            )
            probes.append(_Probe(event, directories[absent], context, plans, required))
        return probes

    def _new_critical_loss(self, candidate, wait):
        # With no baseline route or time for newly matching events, check only the
        # presence of explicit destinations that resolve to an identifiable pair.
        if (candidate.get("match", {}) == self.rule.get("match", {})
                or candidate.get("route", ()) == self.rule.get("route", ())):
            return 0, None
        users = by_id(self.directory["users"])
        pairs = set()
        for kind, _, channel, recipients in self._requirements:
            if kind == "explicit channels" and len(reachable := _reachable(recipients, channel, users)) == 1:
                pairs.add((reachable[0], channel))
        if not pairs:
            return 0, None
        original_match = compile_match(self.rule.get("match", {}))
        report = safety.check(candidate, Ruleset([candidate], self.schemas), self.schemas, self.directory)
        checked = 0
        for scenario in report["scenarios_checked"]:
            fields = scenario["fields"]
            if (not scenario["matched"] or scenario["missing_contact"] is not None
                    or fields["$severity"] != "critical" or original_match(fields)[0]):
                continue
            at = datetime.fromisoformat(scenario["local_time"]).astimezone(UTC)
            plans, critical = self._plan(_probe_event(fields, at), candidate, self.directory, wait)
            if not critical:
                continue
            checked += 1
            if missing := sorted(pairs - plans.keys()):
                recipient, channel = missing[0]
                return checked, {
                    **_boundary_context(scenario, f"new-critical-boundary-{checked}"),
                    "source": "critical_channels", "recipient": recipient, "channel": channel, "kind": "missing",
                }
        return checked, None

    def check(self, candidate, baseline_replay, candidate_replay):
        history = {}
        for event_id in sorted(baseline_replay.critical_events):
            pairs = self._required_baseline(_first_times(baseline_replay, event_id))
            if pairs:
                history[event_id] = pairs

        acted_on = {}
        for alert_id, event_id in self.acted_on:
            if alert_id in acted_on:
                continue
            change = _first_change(
                _first_times(baseline_replay, event_id), _first_times(candidate_replay, event_id),
                alert_id=alert_id, event_id=event_id,
            )
            if change:
                acted_on[alert_id] = change

        summary = {
            "protected_cases": len(self._labels) + len(history) + sum(
                bool(probe.pairs or probe.critical_pairs) for probe in self._boundaries
            ),
            "protected_counts": {
                "labelled_reference": len(self._labels),
                "critical_channels": len(history) + sum(bool(probe.critical_pairs) for probe in self._boundaries),
                "safety_boundary": sum(bool(probe.pairs) for probe in self._boundaries),
            },
            "acted_on_incidents_checked": len({alert_id for alert_id, _ in self.acted_on}),
            "acted_on_incidents_affected": list(acted_on.values()),
            "baseline_limitations": list(self._limitations.values()),
        }
        wait = _scheduled_wait(candidate)
        for probe in self._labels:
            plans, _ = self._plan(probe.event, candidate, probe.directory, wait)
            if loss := _loss(probe.pairs, plans, source="labelled_reference", **probe.context):
                return summary, loss
        for event_id, pairs in history.items():
            if loss := _loss(pairs, _first_times(candidate_replay, event_id),
                             source="critical_channels", event_id=event_id):
                return summary, loss
        for probe in self._boundaries:
            if not probe.pairs and not probe.critical_pairs:
                continue
            plans, _ = self._plan(probe.event, candidate, probe.directory, wait)
            if loss := _loss(probe.critical_pairs, plans, source="critical_channels", **probe.context):
                return summary, loss
            if loss := _loss(probe.pairs, plans, source="safety_boundary", **probe.context):
                return summary, loss
        new_critical_cases, loss = self._new_critical_loss(candidate, wait)
        summary["protected_cases"] += new_critical_cases
        summary["protected_counts"]["critical_channels"] += new_critical_cases
        return summary, loss
