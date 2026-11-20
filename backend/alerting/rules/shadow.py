"""Transaction-scoped live dry-runs of enabled shadow rule revisions."""

import json
from copy import copy

from psycopg.types.json import Jsonb

from alerting.clock import FixedClock
from alerting.contracts import Outcome, Transition
from alerting.rules.engine import episode_for, evaluate, grouping
from alerting.rules.failures import RuleFault, evaluating


class _ShadowState(dict):
    def __init__(self, rule, key, previous, silences_for_rule):
        super().__init__({(rule["id"], key): previous})
        self.silences = silences_for_rule(rule) if silences_for_rule else ()

    def get_silences(self, rule_id):
        return self.silences


async def _record(cur, event_id, rule, outcome, reason):
    await cur.execute(
        """insert into decisions(event_id, rule_id, rule_version, outcome, reason)
           values (%s,%s,%s,%s,%s) on conflict do nothing returning id""",
        (event_id, rule["id"], rule.get("version", 1), outcome.value, Jsonb(reason)),
    )
    return await cur.fetchone() is not None


async def _evaluate_rule(cur, event, ruleset, rule, predicate, clock, resolving, silences_for_rule):
    schema = ruleset.schemas.get(event.schema_id, {})
    with evaluating(rule):
        if not resolving and not predicate(event.fields)[0]:
            return False
        key, _ = grouping(rule, schema, event.fields)
        episode = episode_for(schema, event.fields)
    identity = (rule["id"], rule.get("version", 1), key)
    # Include absent keys in the lock. A simultaneous first match must see the first commit.
    await cur.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 18))", (json.dumps(identity),)
    )
    await cur.execute(
        """select * from shadow_alerts where rule_id=%s and rule_version=%s and alert_key=%s
           and (status='open' or (status='resolved' and latest_episode=%s))
           order by (status='resolved') desc, id desc limit 1""",
        (*identity, None if resolving else episode),
    )
    previous = await cur.fetchone() or {}
    candidate = copy(ruleset)
    candidate.rules = [(rule, predicate)]
    state = _ShadowState(rule, key, previous, silences_for_rule)
    result = evaluate(event, candidate, state, clock)
    decision = result.decisions[0]  # the single draft's decision precedes any event-wide summary
    op = result.alert_ops[0] if result.alert_ops else None
    if not op and decision["outcome"] == Outcome.NO_MATCH and not decision["reason"]["matched"]:
        return False

    count = previous.get("occurrence_count", 0)
    if op and op.transition != Transition.resolved:
        count += 1
    reason = {
        **decision["reason"],
        "shadow": True,
        "alert_key": key,
        "engine_outcome": decision["outcome"],
        "notify_eligible": bool(op and op.notify),
        "occurrence_count": count,
    }
    if not await _record(cur, event.id, rule, Outcome.SHADOW_MATCH, reason):
        return False
    if op is None:
        return True
    if op.transition == Transition.resolved:
        await cur.execute(
            "update shadow_alerts set status='resolved' where id=%s", (previous["id"],)
        )
        return True

    pending = previous.get("count_since_notify", 0)
    if op.notify:
        pending = 0
    elif decision["outcome"] != Outcome.SILENCED:
        pending += 1
    await cur.execute(
        """insert into shadow_alerts(rule_id,rule_version,alert_key,severity,latest_episode,
                                     occurrence_count,count_since_notify,last_notified_at,spike_state)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (rule_id,rule_version,alert_key) where status='open' do update set
             severity=excluded.severity, latest_episode=excluded.latest_episode,
             occurrence_count=excluded.occurrence_count, count_since_notify=excluded.count_since_notify,
             last_notified_at=excluded.last_notified_at, spike_state=excluded.spike_state""",
        (
            *identity,
            (op.effective_severity or event.severity).value,
            episode or previous.get("latest_episode"),
            count,
            pending,
            clock.now() if op.notify else previous.get("last_notified_at"),
            Jsonb(op.spike_state) if op.spike_state is not None else None,
        ),
    )
    return True


async def evaluate_shadow(cur, event, ruleset, clock, *, silences_for_rule=None):
    """Record new SHADOW_MATCH decisions and return their number in the caller's transaction.

    Pass a dict-row cursor, a stored CanonicalEvent and the settings' compiled Ruleset.
    An optional silences_for_rule(draft) supplies the draft owner's compiled silence matchers.
    notify_eligible describes the rule-engine transition before recipient and channel resolution.
    """
    if not ruleset.shadow_rules:
        return 0
    if event.id is None:
        raise ValueError("a shadow decision requires a stored event id")
    clock = FixedClock(clock.now())
    resolve = ruleset.resolvers.get(event.schema_id)
    resolving = resolve and resolve(event.fields)[0]
    matches = 0
    for rule, predicate in sorted(
        ruleset.shadow_rules, key=lambda pair: (pair[0]["id"], pair[0].get("version", 1))
    ):
        try:
            matches += await _evaluate_rule(
                cur, event, ruleset, rule, predicate, clock, resolving, silences_for_rule
            )
        except RuleFault as exc:
            await _record(
                cur, event.id, rule, Outcome.RULE_FAULT, {"shadow": True, "error": str(exc)}
            )
    return matches
