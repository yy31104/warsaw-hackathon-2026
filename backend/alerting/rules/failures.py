"""Classify failures at the boundary that owns them."""

from contextlib import contextmanager
import psycopg


class BadEvent(ValueError):
    pass


class RuleFault(ValueError):
    def __init__(self, rule, exc):
        super().__init__(str(exc))
        self.rule = rule


@contextmanager
def evaluating(rule):
    try:
        yield
    except (ValueError, TypeError, KeyError, ArithmeticError, RuntimeError, AttributeError) as exc:
        raise RuleFault(rule, exc) from exc


def transient(exc):
    return isinstance(exc, psycopg.OperationalError) or getattr(exc, "sqlstate", None) in {
        "40001",
        "40P01",
        "55P03",
        "57014",
        "25P03",
    }
