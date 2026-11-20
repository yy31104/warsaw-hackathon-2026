"""Mask schema-declared values before formatting a notification for an external channel."""

import re

from alerting.templates import lookup


def _id_ranges(ids):
    spans = []
    for alert_id in sorted(ids):
        if spans and alert_id == spans[-1][1] + 1:
            spans[-1][1] = alert_id
        else:
            spans.append([alert_id, alert_id])
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in spans)


def merged_telecom_text(members, channel, event_id=None):
    suffix = " — press 1 to acknowledge" if channel == "phone" else ""
    budget = 4096 - len(suffix)  # fake-telecom Notice.text, in Unicode characters
    remaining = budget - sum(len(m["link"]) for m in members) - (len(members) - 1)
    if remaining >= sum(len(m["title"]) + 1 for m in members):
        return "\n".join(f"{m['title']} {m['link']}" for m in members) + suffix
    if remaining >= 0:
        # Reserve every complete link first, then share the available space among titles.
        lines = []
        for index, member in enumerate(members):
            allowance = max(0, remaining // (len(members) - index) - 1)
            title = member["title"]
            if len(title) > allowance:
                title = title[:allowance - 1] + "…" if allowance else ""
            label = title + " " if title else ""
            lines.append(label + member["link"])
            remaining -= len(label)
        return "\n".join(lines) + suffix

    first_link = members[0]["link"]
    summary = f"{len(members)} alerts\n{first_link}"
    if event_id is not None:
        summary += f"\nView all alerts at http://localhost:8000/v1/events/{event_id}/explain"
    base = first_link.rsplit("/", 1)[0] + "/"
    if all(m["link"] == f"{base}{m.get('alert_id')}" for m in members):
        ids = _id_ranges(m["alert_id"] for m in members)
        compact = f"{summary}\nOpen {base}<ID> for alert IDs {ids}"
        if len(compact) <= budget:
            return compact + suffix
    return summary + suffix


def mask_value(value):
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    if value is None:
        return None
    text = str(value)
    return "****" + (text[-4:] if len(text) > 4 else "")


def mask_external(value, event_fields, sensitive_paths):
    paths = set(sensitive_paths)
    if not paths:
        return value
    replacements = {}
    prefixes = tuple(path + "." for path in paths)

    def collect(item):
        if isinstance(item, dict):
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
        elif item is not None and str(item):
            replacements[str(item)] = mask_value(item)

    for path in paths:
        collect(lookup(event_fields, path))
    # A flattened event has leaves but no parent entry for a declared container.
    for name, item in event_fields.items():
        if name.startswith(prefixes):
            collect(item)
    tokens = sorted(replacements, key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, tokens))) if tokens else None

    def visit(item, path):
        if path in paths or path.startswith(prefixes):
            return mask_value(item)
        if isinstance(item, dict):
            return {
                key: visit(child, f"{path}.{key}" if path else key)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, f"{path}.{index}") for index, child in enumerate(item)]
        if isinstance(item, str) and pattern:
            return pattern.sub(lambda match: replacements[match[0]], item)
        return replacements.get(str(item), item)

    return visit(value, "")
