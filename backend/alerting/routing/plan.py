from alerting.contracts import AlertSnapshot, NotificationIntent, Plan, RenderedMessage
from alerting.delivery.render import mask_external
from alerting.routing.directory import by_id, preferences, target_people, team_for
from alerting.routing.ladder import explicit_targets, initial_routes
from alerting.runbooks.match import match
from alerting.templates import render

KINDS = {
    "created": "initial",
    "severity_increased": "update",
    "cooldown_elapsed": "update",
    "digest_due": "digest",
    "escalation": "escalation",
    "heartbeat_missed": "heartbeat",
    "ladder_step": "ladder_step",
}


def render_message(channel, title, body, fields, excerpt, link, color, meta, event_fields, sensitive_paths):
    masked = bool(sensitive_paths) and channel != "ui"
    if masked:
        title, body, excerpt = mask_external([title, body, excerpt], event_fields, sensitive_paths)
        fields = mask_external(fields, event_fields, sensitive_paths)
    suffix = f" {link}" + (" — press 1 to acknowledge" if channel == "phone" else "")
    short = title[:160 - len(suffix)] + suffix
    if sensitive_paths is not None or channel == "ui":
        meta = {**meta, "masked": masked}
    return RenderedMessage(title=title, body=body if channel in ("email", "ui") else short,
                           short=short, color=color, fields=fields, link=link, meta=meta,
                           runbook_excerpt=excerpt[:512] if excerpt and channel in ("email", "ui") else None)


def plan(alert, transition, rule, event_fields, directory, runbooks, recent_sends, clock, *, sensitive_paths=None):
    if sensitive_paths is None and isinstance(alert, dict):
        sensitive_paths = alert.get("sensitive_paths")
    alert = AlertSnapshot.model_validate(alert)
    book = match(rule["id"], event_fields, runbooks)
    result = Plan(runbook_id=book["id"] if book else None)
    kind = KINDS.get(transition)
    if not kind or (
        rule.get("group", {}).get("mode") == "digest"
        and transition not in ("digest_due", "escalation", "heartbeat_missed", "ladder_step")
    ):
        return result
    if kind == "ladder_step" and alert.status != "open":
        return result
    users, teams = by_id(directory["users"]), by_id(directory["teams"])
    destinations = {}
    explicit = (
        rule.get("_ladder_explicit_targets", ()) if kind == "ladder_step"
        else explicit_targets(rule) if rule.get("ladder") else ()
    )
    protected = {person for target in explicit for person in target_people(target, teams, users)}
    routes = (
        [(route, False) for route in rule.get("route", ())]
        if kind in ("escalation", "ladder_step") else initial_routes(rule)
    )
    for route, ladder in routes:
        target, _, name = route["to"].partition(":")
        team_id = team_for(route["to"], directory)
        team = teams.get(team_id, {})
        if target not in ("user", "oncall", "team"):
            continue
        people = target_people(route["to"], teams, users)
        shared_ui = target == "team"
        if (ladder or kind == "ladder_step") and protected.intersection(people):
            people = [person for person in people if person not in protected]
            shared_ui = False
        requested = route.get("channels", "preferences")
        if shared_ui and team and "ui" in requested:
            destinations[(f"team:{name}", "ui")] = (None, ladder)
        for person in people:
            user = users.get(person)
            if not user:
                continue
            channels = (
                preferences(user, team, directory, alert.severity, clock)
                if requested == "preferences"
                else requested
            )
            for channel in channels:
                recipient = f"team:{name}" if shared_ui and channel == "ui" else person
                contact = user.get("contacts", {}).get(channel)
                if channel == "ui" or contact:
                    key = (recipient, channel)
                    if key not in destinations or not ladder:
                        destinations[key] = (contact, ladder)
    if not destinations and alert.severity == "critical" and kind != "ladder_step":
        fallback = directory.get("fallback_team")
        if fallback in teams:
            destinations[(f"team:{fallback}", "ui")] = (None, False)
    if not destinations:
        result.unrouted_reason = "no recipient or contact resolved"
    link = f"{directory.get('public_ui_url', 'http://localhost:5173')}/a/{alert.id}"
    title = alert.title or alert.rule_id
    body = alert.body or ""
    if kind == "digest":
        body = f"{alert.count_since_notify} occurrences since last digest. {body}".strip()
    epoch = str(alert.occurrence_count) if kind == "initial" else clock.now().isoformat()
    first_notice = kind == "initial" or (kind == "digest" and alert.last_notified_at is None)
    render_fields = {**alert.fields, **event_fields}
    excerpt = None
    if book and any(channel in ("email", "ui") for _, channel in destinations):
        excerpt = render(book["content"], render_fields)
    for (recipient, channel), (contact, ladder) in destinations.items():
        first_step = first_notice and ladder
        intent_kind = "ladder_step" if first_step else kind
        message = render_message(
            channel, title, body, alert.fields, excerpt, link, alert.color,
            {
                "alert_id": alert.id,
                "rule_id": rule["id"],
                "gen_sent_at": event_fields.get("gen_sent_at"),
                **({"ladder_stage": 0, "requires_open_alert": True} if first_step else {}),
            },
            render_fields, sensitive_paths,
        )
        result.intents.append(
            NotificationIntent(
                alert_id=alert.id,
                kind=intent_kind,
                recipient=recipient,
                channel=channel,
                contact=contact,
                rendered=message,
                priority=int((alert.priority or "P3")[1:]) + 1,
                dedup_key=f"{alert.id}:{intent_kind}:{recipient}:{channel}:{epoch}",
            )
        )
    if rule.get("throttle"):
        from .throttle import apply

        for intent in result.intents:
            apply(intent, rule, recent_sends, clock)
    return result
