"""Group only intents already planned for one inbox event in the current transaction."""

from dataclasses import dataclass

from alerting.contracts import NotificationIntent
from alerting.delivery.render import merged_telecom_text


@dataclass
class Member:
    alert: dict
    intent: NotificationIntent


def groups(planned):
    result = []
    by_destination = {}
    seen = set()
    for alert, rule, intents in planned:
        for intent in intents:
            event = alert.get("representative_event_id")
            group = rule.get("merge_group")
            member = Member(alert, intent)
            if not (event is not None and isinstance(group, str) and group.strip()
                    and intent.kind in ("initial", "update")):
                result.append([member])
                continue
            key = event, group, intent.recipient, intent.channel, intent.contact
            rule_key = (*key, alert["rule_id"])
            if rule_key in seen:
                result.append([member])
                continue
            seen.add(rule_key)
            if key not in by_destination:
                by_destination[key] = []
                result.append(by_destination[key])
            by_destination[key].append(member)
    return result


def combined(members):
    """The full wire body lists every member even when it needs multiple SMS segments."""
    first = members[0].intent
    entries = [
        {"alert_id": m.intent.alert_id, "rule_id": m.alert["rule_id"],
         "title": m.intent.rendered.title, "link": m.intent.rendered.link} for m in members
    ]
    summary = f"{len(entries)} alerts"
    event_id = members[0].alert["representative_event_id"]
    telecom = merged_telecom_text(entries, first.channel, event_id)
    if first.channel in ("sms", "phone"):
        body = telecom
    else:
        body = "\n\n".join(
            "\n".join(filter(None, [msg.title, msg.body, msg.runbook_excerpt, msg.link]))
            for msg in (m.intent.rendered for m in members)
        )
    return first.rendered.model_copy(update={
        "title": summary, "body": body, "link": first.rendered.link,
        "short": telecom if len(telecom) <= 160 else f"{summary}. {first.rendered.link}",
        "runbook_excerpt": None,
        "meta": {**first.rendered.meta, "merged_alerts": entries,
                 "event_id": event_id,
                 "masked": any(m.intent.rendered.meta.get("masked") for m in members)},
    })
