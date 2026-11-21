"""Field-name advice for rule authors. The rule always keeps its original paths."""

from collections import defaultdict
from collections.abc import Iterable
from difflib import get_close_matches

from alerting.rules.dsl import compile as compile_match
from alerting.templates import variables


CANONICAL_PATHS = {
    "$source", "$transport", "$schema", "$severity", "$service", "$environment",
    "$error_code", "$message", "$time",
}
_SEPARATORS = str.maketrans("", "", "._-")


def suggest_paths(path: str, known_paths: Iterable[str], limit: int = 3) -> list[str]:
    """Rank close original spellings after folding case and common separators."""
    known = set(known_paths)
    limit = min(limit, 3)
    if path in known or limit <= 0:
        return []
    spellings = defaultdict(list)
    for candidate in sorted(known):
        spellings[candidate.casefold().translate(_SEPARATORS)].append(candidate)
    matches = get_close_matches(path.casefold().translate(_SEPARATORS), spellings, n=limit, cutoff=0.75)
    return [candidate for match in matches for candidate in spellings[match]][:limit]


def _match_scopes(node, metadata):
    """Return scopes where a condition can pass, fail, and be affected by each path.

    Only the schema, source and transport can rule out a scope. Testing other
    predicates against the samples would discard the very events that expose a typo.
    """
    if "not" in node:
        can_pass, can_fail, paths = _match_scopes(node["not"], metadata)
        return can_fail, can_pass, paths
    if "field" in node:
        path = node["field"]
        can_pass, can_fail = set(metadata), set(metadata)
        # Authored regexes are never run against producer-provided names by this
        # advisory. Other selectors can still narrow the scope of the same branch.
        if path in {"$schema", "$source", "$transport"} and node["op"] != "regex":
            predicate = compile_match(node)
            for key, fields in metadata.items():
                if fields[path] is not None or path == "$schema":
                    (can_fail if predicate(fields)[0] else can_pass).discard(key)
        return can_pass, can_fail, {path: set(metadata)}

    if "any" in node:
        children = node["any"]
    elif "all" in node:
        children = node["all"]
    else:
        children = [{"field": path, "op": "eq", "value": value} for path, value in node.items()]
    conjunction = "any" not in node
    can_pass, can_fail = (set(metadata), set()) if conjunction else (set(), set(metadata))
    references = defaultdict(set)
    for child in children:
        passes, fails, paths = _match_scopes(child, metadata)
        if conjunction:
            can_pass.intersection_update(passes)
            can_fail.update(fails)
        else:
            can_pass.update(passes)
            can_fail.intersection_update(fails)
        for path, eligible in paths.items():
            references[path].update(eligible)
    # A false sibling fixes an AND, and a true sibling fixes an OR.
    influential = can_pass if conjunction else can_fail
    return can_pass, can_fail, {path: eligible & influential for path, eligible in references.items()}


def _output_paths(rule):
    for section, key in (("group", "by"), ("present", "fields"), ("expect", "key")):
        settings = rule.get(section)
        if isinstance(settings, dict) and isinstance(settings.get(key), list):
            yield from (path for path in settings[key] if isinstance(path, str))
    present = rule.get("present") or {}
    for key in ("title", "body"):
        if isinstance(present.get(key), str):
            yield from variables(present[key])


def sample_filters(rule):
    """Sample each registry independently, including sparse names targeted by the rule."""
    try:
        _, leaves = compile_match(rule.get("match", {}))({})
    except (TypeError, ValueError):
        # Invalid predicates are reported by the safety checker.
        return []
    references = {leaf["field"] for leaf in leaves} | set(_output_paths(rule))
    if references <= CANONICAL_PATHS:
        return []
    filters = {(False, ())}
    for leaf in leaves:
        if leaf["field"] != "$schema" or leaf["op"] not in {"eq", "neq", "in", "not_in", "glob"}:
            continue
        values = leaf["value"] if leaf["op"] in {"in", "not_in"} else [leaf["value"]]
        patterns = []
        for value in values:
            if not isinstance(value, str):
                continue
            pattern = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            if leaf["op"] == "glob":
                # LIKE is a coarse inventory filter. The DSL checks the original glob exactly.
                pattern = pattern.split("[", 1)[0] + "%" if "[" in pattern else pattern
                pattern = pattern.replace("*", "%").replace("?", "_")
            patterns.append(pattern)
            filters.add((False, (pattern,)))
        if patterns:
            # A negated condition may depend on names outside this set.
            filters.add((True, tuple(sorted(set(patterns)))))
    return [{"exclude": exclude, "patterns": patterns} for exclude, patterns in sorted(filters)]


def unknown_fields(rule, schemas, samples):
    """Use only path names from eligible samples and their schema alias mappings."""
    registry = {schema["id"]: schema for schema in schemas}
    observed = {schema_id for schema_id, *_ in samples}
    inventories = {
        (schema_id, name, source, transport): set(paths)
        for schema_id, name, source, transport, paths in samples
    }
    for schema_id, schema in registry.items():
        if schema_id not in observed:
            # A static name is known before the first event. A template needs a sample.
            name = schema.get("name")
            inventories[schema_id, None if variables(name) else name, None, None] = set()

    metadata = {}
    for key, paths in inventories.items():
        schema_id, name, source, transport = key
        metadata[key] = {"$schema": name, "$source": source, "$transport": transport}
        schema = registry.get(schema_id, {})
        paths.update(schema.get("aliases", {}).values())
        if schema.get("episode_key"):
            paths.add("$episode")

    possible, _, references = _match_scopes(rule.get("match", {}), metadata)
    for path in _output_paths(rule):
        references.setdefault(path, set()).update(possible)

    problems = []
    for path, scopes in sorted(references.items()):
        known = CANONICAL_PATHS.union(*(inventories[scope] for scope in scopes))
        if path not in known:
            problems.append({
                "code": "unknown_field", "path": path, "suggestions": suggest_paths(path, known),
                "message": f"Field '{path}' was not found in recent events or configured aliases for this rule.",
            })
    return problems
