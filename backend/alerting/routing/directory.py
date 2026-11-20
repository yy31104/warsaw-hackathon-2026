from zoneinfo import ZoneInfo

from alerting.contracts import SEVERITY_ORDER


def by_id(entries):
    return {entry["id"]: entry for entry in entries}


def team_for(owner, directory):
    kind, _, name = owner.partition(":")
    if kind in ("team", "oncall"):
        return name
    return next(
        (t["id"] for t in directory["teams"] if name in t.get("members", [])),
        directory.get("fallback_team"),
    )


def preferences(user, team, directory, severity, clock):
    local = clock.now().astimezone(ZoneInfo(user.get("timezone", "UTC"))).strftime("%H:%M")
    for rows in (
        user.get("preferences", []),
        team.get("preferences", []),
        directory.get("default_preferences", []),
    ):
        for row in rows:
            if SEVERITY_ORDER.get(severity, -1) < SEVERITY_ORDER.get(
                row.get("min_severity", "debug"), 0
            ):
                continue
            if "when" in row:
                start, end = row["when"].split("-")
                inside = start <= local < end if start <= end else local >= start or local < end
                if not inside:
                    continue
            return row["channels"]
    return []
