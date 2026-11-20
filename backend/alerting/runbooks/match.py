from functools import lru_cache

import yaml
from alerting.rules.dsl import compile


@lru_cache(maxsize=128)
def yaml_spec(content):
    return yaml.safe_load(content) or {}


def match(rule_id, fields, runbooks):
    for book in runbooks:
        if rule_id in book.get("rules", []):
            return book
        criteria = book.get("matches")
        if criteria is None and book.get("format") == "yaml-steps":
            criteria = yaml_spec(book["content"]).get("matches")
        if criteria and compile(criteria)(fields)[0]:
            return book
    return None
