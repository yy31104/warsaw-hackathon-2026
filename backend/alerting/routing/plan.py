from alerting.contracts import AlertSnapshot, NotificationIntent, Plan, RenderedMessage
from alerting.routing.directory import by_id, preferences, team_for
from alerting.runbooks.match import match

KINDS = {"created": "initial", "severity_increased": "update", "cooldown_elapsed": "update",
         "digest_due": "digest", "escalation": "escalation"}


def plan(alert, transition, rule, event_fields, directory, runbooks, recent_sends, clock):
    alert = AlertSnapshot.model_validate(alert)
    book = match(rule["id"], event_fields, runbooks)
    result = Plan(runbook_id=book["id"] if book else None)
    kind = KINDS.get(transition)
    if not kind or (rule.get("group", {}).get("mode") == "digest" and transition not in ("digest_due", "escalation")):
        return result
    users, teams = by_id(directory["users"]), by_id(directory["teams"])
    destinations = {}
    for route in rule.get("route", []):
        target, _, name = route["to"].partition(":")
        team_id = team_for(route["to"], directory)
        team = teams.get(team_id, {})
        if target == "user":
            people = [name] if name in users else []
        elif target == "oncall":
            people = [team.get("oncall", {}).get("primary")]
        elif target == "team":
            people = team.get("members", [])
        else:
            continue
        requested = route.get("channels", "preferences")
        if target == "team" and team and "ui" in requested:
            destinations[(f"team:{name}", "ui")] = None
        for person in people:
            user = users.get(person)
            if not user:
                continue
            channels = preferences(user, team, directory, alert.severity, clock) if requested == "preferences" else requested
            for channel in channels:
                recipient = f"team:{name}" if target == "team" and channel == "ui" else person
                contact = user.get("contacts", {}).get(channel)
                if channel == "ui" or contact:
                    destinations[(recipient, channel)] = contact
    if not destinations and alert.severity == "critical":
        fallback = directory.get("fallback_team")
        if fallback in teams:
            destinations[(f"team:{fallback}", "ui")] = None
    if not destinations:
        result.unrouted_reason = "no recipient or contact resolved"
    link = f"http://localhost:5173/a/{alert.id}"
    title = alert.title or alert.rule_id
    body = alert.body or ""
    if kind == "digest":
        body = f"{alert.count_since_notify} occurrences since last digest. {body}".strip()
    epoch = str(alert.occurrence_count) if kind == "initial" else clock.now().isoformat()
    for (recipient, channel), contact in destinations.items():
        suffix = f" {link}" + (" — press 1 to acknowledge" if channel == "phone" else "")
        short = title[:160-len(suffix)] + suffix
        message = RenderedMessage(title=title, body=body if channel in ("email", "ui") else short, short=short,
                                  color=alert.color, fields=alert.fields, link=link,
                                  runbook_excerpt=book["content"][:512] if book and channel in ("email", "ui") else None,
                                  meta={"alert_id": alert.id, "rule_id": rule["id"],
                                        "gen_sent_at": event_fields.get("gen_sent_at")})
        result.intents.append(NotificationIntent(alert_id=alert.id, kind=kind, recipient=recipient,
            channel=channel, contact=contact, rendered=message, priority=int((alert.priority or "P3")[1:]) + 1,
            dedup_key=f"{alert.id}:{kind}:{recipient}:{channel}:{epoch}"))
    return result
