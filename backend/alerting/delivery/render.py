"""Mask schema-declared values before formatting a notification for an external channel."""

import re

from alerting.templates import lookup


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
            if name.startswith(path + "."):
                collect(item)
    tokens = sorted(replacements, key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, tokens))) if tokens else None

    def visit(item, path):
        if path in paths:
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
