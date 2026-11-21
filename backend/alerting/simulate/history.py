"""Replay stored canonical events through the production evaluator and routing planner."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from heapq import heappop, heappush

from alerting.clock import SYSTEM, FixedClock, clock_for_event
from alerting.contracts import Transition
from alerting.routing import plan
from alerting.routing.directory import team_for
from alerting.routing.event_policy import eligible_ruleset
from alerting.routing.throttle import window
from alerting.rules.engine import Ruleset, duration, episode_for, evaluate
from alerting.rules.heartbeat import heartbeat_deadline


def historical_time(event):
    clock = clock_for_event(event.trace)
    return event.received_at if clock is SYSTEM else clock.now()


def alert_snapshot(op, event, directory, *, id):
    return {
        "id": id, "rule_id": op.rule["id"], "rule_version": op.rule.get("version", 1), "alert_key": op.key,
        "status": "open", "severity": op.effective_severity or event.severity,
        "title": op.title, "body": op.body, "fields": op.fields, "color": op.rule.get("present", {}).get("color"),
        "team_id": team_for(op.rule.get("owner", ""), directory),
    }


@dataclass
class Replay:
    rule: dict
    ruleset: Ruleset
    directory: dict
    runbooks: list
    alerts: int = 0
    matched_events: int = 0
    notifications: Counter = field(default_factory=Counter)
    recipients: dict = field(default_factory=lambda: defaultdict(set))
    open_alerts: dict = field(default_factory=dict)
    resolved: dict = field(default_factory=dict)
    digests: list = field(default_factory=list)
    seen_notifications: set = field(default_factory=set)
    window_counts: dict = field(default_factory=lambda: defaultdict(Counter))

    def get_open(self, rule_id, key):
        return self.open_alerts.get(key)

    def get_resolved_episode(self, rule_id, key, episode):
        return self.resolved.get((key, episode))

    def route(self, row, transition, event_ids, at):
        clock = FixedClock(at)
        counts = self.window_counts[window(self.rule, clock)[0]] if self.rule.get("throttle") else Counter()
        routed = plan(row, transition, self.rule, row["event_fields"], self.directory, self.runbooks, counts, clock)
        for intent in routed.intents:
            pair = (intent.recipient, intent.channel.value)
            counts[pair] += 1
            key = intent.rendered.meta.get("throttle_bucket") or intent.dedup_key
            if key not in self.seen_notifications:
                self.seen_notifications.add(key)
                self.notifications["/".join(pair)] += 1
            for event_id in event_ids:
                self.recipients[event_id].add(intent.recipient)
        if routed.intents:
            row.update(last_notified_at=at, count_since_notify=0)
        return bool(routed.intents)

    def due_digests(self, through):
        while self.digests and self.digests[0][0] <= through:
            due, id, key = heappop(self.digests)
            row = self.open_alerts.get(key)
            if row is not None and row["id"] == id:
                row["next_digest_at"] = None
                if self.route(row, Transition.digest_due, row["digest_events"], due):
                    row["digest_events"].clear()

    def apply(self, event, at):
        self.due_digests(at)
        evaluation = evaluate(event, eligible_ruleset(event, self.ruleset), self, FixedClock(at))
        self.matched_events += any(d["reason"].get("matched") for d in evaluation.decisions)
        for op in evaluation.alert_ops:
            row = self.open_alerts.get(op.key)
            if op.transition == Transition.resolved:
                if row["latest_episode"] is not None:
                    self.resolved[op.key, row["latest_episode"]] = row["id"]
                del self.open_alerts[op.key]
                continue
            if row is None:
                self.alerts += 1
                row = self.open_alerts[op.key] = {
                    "id": self.alerts, "first_seen_at": at, "last_notified_at": None, "latest_episode": None,
                    "occurrence_count": 0, "count_since_notify": 0, "next_digest_at": None, "digest_events": [],
                }
            row.update(alert_snapshot(op, event, self.directory, id=row["id"]))
            schema = self.ruleset.schemas.get(event.schema_id, {})
            episode = episode_for(schema, event.fields)
            row.update(
                latest_episode=episode if episode is not None else row["latest_episode"],
                spike_state=op.spike_state, last_seen_at=at, event_fields=event.fields,
                occurrence_count=row["occurrence_count"] + 1, count_since_notify=row["count_since_notify"] + 1,
            )
            if op.notify:
                self.route(row, op.routing_transition or op.transition, [event.id], at)
            group = self.rule.get("group", {})
            if group.get("mode") == "digest":
                row["digest_events"].append(event.id)
                if row["next_digest_at"] is None:
                    every = duration(group.get("every", "5m"))
                    if every <= timedelta():
                        raise ValueError("digest interval must be positive")
                    due = max(at, (row["last_notified_at"] or row["first_seen_at"]) + every)
                    row["next_digest_at"] = due
                    heappush(self.digests, (due, row["id"], op.key))

    def run(self, timed):
        if not timed or not self.ruleset.rules:
            return
        if "expect" in self.rule:
            self.replay_heartbeats(timed)
            return
        for at, event in timed:
            self.apply(event, at)
        self.due_digests(timed[-1][0])

    def replay_heartbeats(self, timed):
        watches, missed = {}, []
        for at, event in timed:
            for rule, predicate in eligible_ruleset(event, self.ruleset).rules:
                matches, _ = predicate(event.fields)
                if not matches:
                    continue
                self.matched_events += 1
                schema = self.ruleset.schemas.get(event.schema_id, {})
                key, _, due = heartbeat_deadline(rule["expect"], schema, event.fields, at)
                previous = watches.get(key)
                # A beat exactly on its own deadline renews that watch first.
                if previous and previous[0] < at:
                    missed.append(previous)
                watches[key] = (due, key, event)

        through = timed[-1][0]
        missed.extend(watch for watch in watches.values() if watch[0] <= through)
        for due, key, event in sorted(missed, key=lambda item: (item[0], item[1])):
            self.alerts += 1
            row = {
                "id": self.alerts, "rule_id": self.rule["id"], "rule_version": self.rule.get("version", 1),
                "alert_key": key, "status": "open", "severity": self.rule.get("severity", "warning"),
                "team_id": team_for(self.rule.get("owner", ""), self.directory),
                "event_fields": event.fields, "occurrence_count": 1,
            }
            self.route(row, Transition.heartbeat_missed, [event.id], due)

    def summary(self):
        return {"alerts": self.alerts, "matched_events": self.matched_events,
                "notifications": dict(sorted(self.notifications.items()))}


def preview_history(events, current, draft, schemas, directory, runbooks):
    timed = sorted(((historical_time(event), event) for event in events), key=lambda pair: (pair[0], pair[1].id))
    prior = Replay(current or {}, Ruleset([current] if current else [], schemas), directory, runbooks)
    proposed = Replay(draft, Ruleset([draft], schemas), directory, runbooks)
    prior.run(timed)
    proposed.run(timed)

    before = set().union(*prior.recipients.values())
    after = set().union(*proposed.recipients.values())
    lost_all, losses = [], []
    for _, event in timed:
        received = prior.recipients.get(event.id, set())
        retained = proposed.recipients.get(event.id, set())
        if removed := sorted(received - retained):
            losses.append({"event_id": event.id, "recipients": removed})
            if not retained:
                lost_all.append(event.id)
    return {
        "rule_id": draft["id"], "base_version": current.get("version", 1) if current else None,
        "events_replayed": len(timed), "starts_from_empty_state": True,
        "replay_basis": "Selected rule, stored event fields and times, current directory, event-driven sends, digests and missed heartbeat deadlines within the sample.",
        "current": prior.summary(), "draft": proposed.summary(),
        "lost_all_notifications": lost_all, "lost_recipients": sorted(before - after), "new_recipients": sorted(after - before),
        "events_with_recipient_loss": losses,
        "sample_event_ids": [event.id for _, event in timed[:20]],
    }
