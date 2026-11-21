"""Durable USD reservations, receipts, and immutable case results for one M3 campaign."""
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal


NANO = 1_000_000_000
LIMIT = 5 * NANO


def usd(nanos):
    return format(Decimal(nanos) / NANO, "f")


def dated_snapshot(family, model):
    if not isinstance(model, str) or not re.fullmatch(re.escape(family) + r"-[0-9]{4}-[0-9]{2}-[0-9]{2}", model):
        return False
    try:
        date.fromisoformat(model[-10:])
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Rate:
    input: Decimal
    cached: Decimal
    output: Decimal
    context: int
    write: Decimal
    long_above: int

    @classmethod
    def from_dict(cls, data):
        prices = [Decimal(str(data[k])) for k in
                  ("input_per_million", "cached_input_per_million", "output_per_million")]
        context = data["max_context_tokens"]
        if (not all(p.is_finite() and p >= 0 for p in prices)
                or min(prices[0], prices[2]) == 0 or prices[1] > prices[0]
                or type(context) is not int or context <= 0):
            raise ValueError("invalid rate card")
        write = Decimal(str(data.get("cache_write_per_million", prices[0])))
        threshold = data.get("long_context_above", context)
        if not write.is_finite() or write < prices[0] or type(threshold) is not int or threshold <= 0:
            raise ValueError("invalid cache-write or long-context rate")
        return cls(*prices, context, write, threshold)

    def cost(self, inputs, cached, outputs, written=0):
        # USD/million tokens × 1000 = nanodollars/token. Always round up.
        input_total = (inputs - cached - written) * self.input + cached * self.cached + written * self.write
        output_total = outputs * self.output
        if inputs > self.long_above:
            input_total *= 2
            output_total *= Decimal("1.5")
        value = input_total + output_total
        return int((value * 1000).to_integral_value(rounding=ROUND_CEILING))


class Journal:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS phases (
                    name TEXT PRIMARY KEY, manifest TEXT NOT NULL, summary TEXT);
                CREATE TABLE IF NOT EXISTS cases (
                    phase TEXT NOT NULL, mode TEXT NOT NULL, case_id TEXT NOT NULL, result TEXT,
                    PRIMARY KEY (phase, mode, case_id));
                CREATE TABLE IF NOT EXISTS calls (
                    phase TEXT NOT NULL, model TEXT NOT NULL, case_id TEXT NOT NULL,
                    reserved INTEGER NOT NULL, liability INTEGER NOT NULL,
                    output_cap INTEGER NOT NULL, receipt TEXT, PRIMARY KEY (phase, model, case_id));
                CREATE TABLE IF NOT EXISTS preflights (
                    phase TEXT NOT NULL, model TEXT NOT NULL, case_id TEXT NOT NULL, counted INTEGER,
                    input_bound INTEGER NOT NULL, latency_ms REAL, breached INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (phase, model, case_id));
            """)

    @contextmanager
    def tx(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def phase(self, name):
        with self.tx() as db:
            row = db.execute("SELECT manifest,summary FROM phases WHERE name=?", (name,)).fetchone()
        return (json.loads(row[0]), json.loads(row[1]) if row[1] else None) if row else (None, None)

    def start(self, name, manifest):
        frozen = json.dumps(manifest, sort_keys=True)
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO phases(name,manifest) VALUES (?,?)", (name, frozen))
            original, = db.execute("SELECT manifest FROM phases WHERE name=?", (name,)).fetchone()
            if original != frozen:
                raise ValueError(f"{name} already started with different cases, code, models or settings")

    def finish(self, name, summary):
        with self.tx() as db:
            db.execute("UPDATE phases SET summary=COALESCE(summary,?) WHERE name=?", (json.dumps(summary), name))

    def claim(self, phase, mode, case_id):
        key = (phase, mode, case_id)
        with self.tx() as db:
            inserted = db.execute("INSERT OR IGNORE INTO cases VALUES (?,?,?,NULL)", key).rowcount
            payload, = db.execute("SELECT result FROM cases WHERE phase=? AND mode=? AND case_id=?", key).fetchone()
        return bool(inserted), json.loads(payload) if payload else None

    def save(self, phase, mode, case_id, row):
        with self.tx() as db:
            db.execute("UPDATE cases SET result=COALESCE(result,?) WHERE phase=? AND mode=? AND case_id=?",
                       (json.dumps(row), phase, mode, case_id))

    def exposure(self):
        with self.tx() as db:
            return db.execute("SELECT COALESCE(SUM(liability),0) FROM calls").fetchone()[0]

    @contextmanager
    def campaign_lock(self):
        # Separate DB so the long-lived run lock does not block durable reservations.
        with closing(sqlite3.connect(str(self.path) + ".lock", timeout=0)) as db:
            try:
                db.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as e:
                raise ValueError("another evaluation is using this campaign journal") from e
            yield

    def reserve(self, phase, model, case_id, rate, output_cap, *, counted=None, count_ms=None):
        if counted is not None and (type(counted) is not int or not 0 < counted <= rate.context):
            raise ValueError("token-count preflight returned an invalid count")
        # The free SDK endpoint counts the exact serialized M2 input, including the
        # strict schema. Add 1024 tokens or 25% (whichever is larger) for variance.
        bound = rate.context if counted is None else min(rate.context, counted + max(1024, (counted + 3) // 4))
        reserved = rate.cost(bound, 0, output_cap, written=bound)
        with self.tx() as db:
            if db.execute("SELECT 1 FROM preflights WHERE breached=1 LIMIT 1").fetchone():
                raise ValueError("provider exceeded a prior token bound; campaign stopped")
            exposure, = db.execute("SELECT COALESCE(SUM(liability),0) FROM calls").fetchone()
            if exposure + reserved > LIMIT:
                raise ValueError("cumulative $5 budget cannot cover the next worst-case call")
            try:
                db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,NULL)",
                           (phase, model, case_id, reserved, reserved, output_cap))
                db.execute("INSERT INTO preflights VALUES (?,?,?,?,?,?,0)",
                           (phase, model, case_id, counted, bound, count_ms))
            except sqlite3.IntegrityError as e:
                raise ValueError("call already reserved for this case and mode") from e
        return reserved

    def settle(self, phase, model, case_id, rate, obs):
        key = (phase, model, case_id)
        with self.tx() as db:
            reserved, cap, receipt = db.execute(
                "SELECT reserved,output_cap,receipt FROM calls WHERE phase=? AND model=? AND case_id=?", key
            ).fetchone()
            if receipt:
                raise ValueError("call already settled")
            counted, bound, count_ms = db.execute(
                "SELECT counted,input_bound,latency_ms FROM preflights WHERE phase=? AND model=? AND case_id=?", key
            ).fetchone()
            i, c, o = obs.input_tokens, obs.cached_input_tokens, obs.output_tokens
            match = "exact" if obs.response_model == model else (
                "dated_snapshot" if dated_snapshot(model, obs.response_model) else None)
            identity = obs.provider == "openai" and obs.model == model and match is not None
            # Treat missing cache detail as uncached for pricing. Missing totals or a different
            # served model leave the entire original reservation outstanding.
            writes = getattr(obs, "cache_write_input_tokens", None)
            cache = 0 if c is None else c
            # With no write breakdown, price all non-read tokens at the higher write rate.
            written = (i - cache if type(i) is int and type(cache) is int else None) if writes is None else writes
            known = (identity and type(i) is int and type(o) is int and type(cache) is int
                     and type(written) is int and 0 <= cache <= i <= rate.context and 0 <= o <= cap
                     and 0 <= written <= i - cache)
            cost = rate.cost(i, cache, o, written) if known else None
            liability = reserved if cost is None else cost
            if (type(i) is int and i > bound) or (type(o) is int and o > cap):
                # Preserve reported usage and stop subsequent calls on a broken provider contract.
                db.execute("UPDATE preflights SET breached=1 WHERE phase=? AND model=? AND case_id=?", key)
                if cost is None:
                    liability = max(reserved, rate.cost(rate.context, 0, max(o or 0, cap), written=rate.context))
            detail = dict(provider=obs.provider, requested_model=obs.model, response_model=obs.response_model,
                          model_verified=identity, identity_match=match, outcome=obs.outcome, input_tokens=i,
                          cached_input_tokens=c, cache_write_input_tokens=writes, output_tokens=o,
                          cost_basis=("upper_bound" if c is None or writes is None else "observed_mix") if known else None,
                          latency_ms=obs.latency_ms,
                          preflight_input_tokens=counted, reserved_input_tokens=bound, preflight_latency_ms=count_ms,
                          estimated_cost_usd=usd(cost) if cost is not None else None,
                          reserved_usd=usd(reserved), accounted_usd=usd(liability))
            db.execute("UPDATE calls SET liability=?,receipt=? WHERE phase=? AND model=? AND case_id=?",
                       (liability, json.dumps(detail), *key))
            return detail

    def receipt(self, phase, mode, case_id):
        with self.tx() as db:
            value = db.execute("""SELECT reserved,receipt,counted,input_bound,latency_ms
                FROM calls LEFT JOIN preflights USING (phase,model,case_id) WHERE phase=? AND model=? AND case_id=?""",
                               (phase, mode, case_id)).fetchone()
        if value is None:
            return None
        if value[1]:
            return json.loads(value[1])
        return dict(provider="openai", requested_model=mode, response_model=None, model_verified=False,
                    outcome="unconfirmed", input_tokens=None, cached_input_tokens=None, output_tokens=None,
                    preflight_input_tokens=value[2], reserved_input_tokens=value[3], preflight_latency_ms=value[4],
                    latency_ms=None, estimated_cost_usd=None, reserved_usd=usd(value[0]), accounted_usd=usd(value[0]))
