"""Run all golden cases against a disposable DEMO_MODE stack.

From the repository root: backend/.venv/bin/python loadtest/golden_runner.py
URLs come from ALERTING_API_URL, FAKE_TELECOM_URL and MAILPIT_URL. --broker enables
publishing to the queues in sources.yaml using RABBITMQ_URL. No broker is contacted
by default. --log-input selects an existing file watched by this stack's log tailer.
Each case resets the demo API and replays its declared ancestors. Exit codes:
0 all passed, 1 at least one failure, 2 incomplete. Only docs/GATES.md is updated.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "fixtures/golden/expectations.yaml"
GATES = ROOT / "docs/GATES.md"
TERMINAL = {"processed", "quarantined", "needs_review", "rejected"}


class Mismatch(Exception):
    """An observation differs from the fixture."""


class Unavailable(Exception):
    """A prerequisite for a measurement is unavailable."""


class Checks:
    """Retain independently observable failures even when another service is missing."""

    def __init__(self):
        self.errors = []

    @contextmanager
    def measure(self, label=""):
        try:
            yield
        except (Mismatch, Unavailable, KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
            kind = Unavailable if isinstance(exc, Unavailable) else Mismatch
            self.errors.append(kind(f"{label}: {exc}" if label else str(exc)))

    def finish(self):
        if self.errors:
            kind = Mismatch if any(isinstance(e, Mismatch) for e in self.errors) else Unavailable
            raise kind(" / ".join(str(e) for e in self.errors))


@dataclass(frozen=True)
class Result:
    case_id: str
    status: str
    detail: str


def equal(actual, expected, label):
    if actual != expected:
        raise Mismatch(f"{label}: expected {expected!r}, observed {actual!r}")


def require(condition, message):
    if not condition:
        raise Mismatch(message)


def subset(actual, expected, label):
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{label}: expected an object, observed {actual!r}")
        for key, value in expected.items():
            require(key in actual, f"{label}: missing {key!r}")
            subset(actual[key], value, f"{label}.{key}")
    else:
        equal(actual, expected, label)


def rows(data, key="items"):
    require(isinstance(data, (list, dict)), f"Expected a list or an object containing {key!r}")
    value = data if isinstance(data, list) else data.get(key)
    require(isinstance(value, list), f"Expected a list in {key!r}")
    return value


def merge(original, changes):
    result = deepcopy(original)
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = merge(result[k], v)
        else:
            result[k] = deepcopy(v)
    return result


def payload(spec):
    body = spec["body"] if "body" in spec else json.loads((ROOT / spec["file"]).read_text())
    return merge(body, spec.get("mutate", {}))


def lineage(case, cases):
    chain = [case]
    while parent := chain[-1].get("after"):
        require(parent in cases and all(c["id"] != parent for c in chain),
                f"Invalid after dependency {parent!r}")
        chain.append(cases[parent])
    return reversed(chain)


def wait_for(check, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except Mismatch:
            if time.monotonic() >= deadline:
                raise
        time.sleep(min(.2, max(0, deadline - time.monotonic())))


async def publish(queue, messages):
    import aio_pika

    try:
        url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
        async with await aio_pika.connect(url, timeout=4) as connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.get_queue(queue, ensure=True)
            for body, headers, message_id in messages:
                message = aio_pika.Message(
                    body=json.dumps(body).encode(), headers={k.lower(): v for k, v in headers.items()},
                    message_id=message_id, content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                )
                await channel.default_exchange.publish(message, routing_key=queue, mandatory=True, timeout=4)
    except (OSError, aio_pika.exceptions.AMQPException, asyncio.TimeoutError) as exc:
        raise Unavailable(f"RabbitMQ queue {queue} unavailable ({type(exc).__name__})") from exc


class Runner:
    def __init__(self, client, *, api, telecom, mailpit, timeout=10, broker=False, log_input=None):
        self.client = client
        self.api, self.telecom, self.mailpit = (u.rstrip("/") for u in (api, telecom, mailpit))
        self.timeout = timeout
        self.broker = broker
        self.log_input = log_input
        sources = yaml.safe_load((ROOT / "config/sources.yaml").read_text())["sources"]
        self.sources = {s["id"]: s for s in sources}
        users = yaml.safe_load((ROOT / "config/teams.yaml").read_text())["users"]
        self.contacts = {u["id"]: u.get("contacts", {}) for u in users}
        self.rules = yaml.safe_load((ROOT / "config/rules.yaml").read_text())["rules"]

    def http(self, method, path, *, base=None, status_only=False, **kwargs):
        label = f"{method} {path}"
        try:
            reply = self.client.request(method, (base or self.api) + path, **kwargs)
        except httpx.RequestError as exc:
            raise Unavailable(f"{label} unavailable ({type(exc).__name__})") from exc
        status = reply.status_code
        try:
            data = reply.json() if reply.content else {}
        except ValueError:
            data = {}
            if reply.is_success and not status_only:
                raise Mismatch(f"{label} returned invalid JSON")
        detail = data.get("detail") if isinstance(data, dict) else None
        # A missing route and a deliberate 404 from an approval handler are different observations.
        if (status == 404 and (detail in (None, "Not Found", "unknown webhook source"))
                or status in (405, 501)
                or base is not None and status in (502, 503, 504)
                or path == "/v1/debug/reset" and status in (401, 403, 404, 502, 503, 504)):
            raise Unavailable(f"{label} returned HTTP {status}" + (f" ({detail})" if detail else ""))
        if status_only:
            return status, data
        if not reply.is_success:
            raise Mismatch(f"{label} returned HTTP {status}" + (f" ({detail})" if detail else ""))
        return data

    def page(self, path, *, base=None, params=None, key="items", offset_key="offset"):
        # The API and Mailpit use different offset names. Stop rather than counting a page twice.
        out, seen = [], set()
        while True:
            data = self.http("GET", path, base=base,
                             params={**(params or {}), "limit": 100, offset_key: len(out)})
            batch = rows(data, key)
            if not batch:
                break
            ids = [row.get("id", row.get("ID")) for row in batch]
            require(None not in ids and not seen.intersection(ids), f"{path} repeated a page or omitted ids")
            out.extend(batch)
            seen.update(ids)
            total = data.get("total") if isinstance(data, dict) else None
            if (total is not None and len(out) >= total) or (total is None and len(batch) < 100):
                break
        return out

    def run(self, suite):
        cases = {c["id"]: c for c in suite["cases"]}
        require(len(cases) == len(suite["cases"]), "Duplicate golden case ids")
        results = []
        for case in suite["cases"]:
            try:
                self.http("POST", "/v1/debug/reset")
                trial = Trial(self, suite["clock_default"])
                trial.run(case, cases)
                result = Result(case["id"], "PASS", "All declared checks passed")
            except Unavailable as exc:
                result = Result(case["id"], "NOT_RUN", str(exc))
            except (Mismatch, KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
                result = Result(case["id"], "FAIL", str(exc))
            results.append(result)
        return results


class Trial:
    def __init__(self, runner, clock):
        self.r = runner
        self.clock = clock
        self.seen_ids = set()
        self.explains = {}
        self.ingest = Counter()
        self.response = {}
        self.http_status = None
        self.action = {}
        self.receivers_before = {}
        self.before_outage = {}
        self.before_counts = {}
        self.checked_samples = []
        self.funnel_before = None

    def api(self, path, **kwargs):
        return self.r.http("GET", "/v1" + path, **kwargs)

    def telecom(self, method, path, **kwargs):
        return self.r.http(method, path, base=self.r.telecom, **kwargs)

    def mail(self, path):
        return self.r.http("GET", path, base=self.r.mailpit)

    def alerts(self):
        return rows(self.api("/alerts"))

    def one_alert(self):
        items = self.alerts()
        require(len(items) == 1, f"Expected one alert for this operation, observed {len(items)}")
        return items[0]

    def notifications(self):
        return rows(self.api("/notifications"))

    def run(self, case, cases):
        chain = list(lineage(case, cases))
        # Detect disabled ingress before touching the receivers or changing their fault mode.
        for ancestor in chain:
            specs = [ancestor[k] for k in ("generate", "send") if k in ancestor]
            specs += ancestor.get("send_batch", [])
            specs += [s["send"] for s in ancestor.get("steps", [])]
            if not self.r.broker and any(self.r.sources[s["source"]]["transport"] == "rabbitmq" for s in specs):
                raise Unavailable("RabbitMQ broker publishing disabled (enable with --broker)")
        self.snapshot_receivers(case)
        for ancestor in chain[:-1]:
            try:
                self.perform(ancestor, verify=False)
            except (Mismatch, Unavailable) as exc:
                raise Unavailable(f"Prerequisite {ancestor['id']}: {exc}") from exc
        self.perform(case, verify=True)

    def perform(self, case, verify):
        expect = case["expect"]
        before_mode = None
        checks = Checks()
        if "setup" in case:
            before_mode = self.telecom("GET", "/mode")["mode"]
        try:
            if before_mode is not None:
                self.set_mode(case["setup"]["fake_telecom_mode"])
            if "occurrence_count_delta" in expect.get("alert", {}):
                self.before_counts = {a["id"]: a["occurrence_count"] for a in self.alerts()}
            if "ingest" in expect and self.r.sources[case["send"]["source"]]["transport"] == "rabbitmq":
                self.funnel_before = self.api("/stats/funnel")
            for key in ("send", "send_batch", "generate", "steps"):
                if key not in case:
                    continue
                if key == "steps":
                    for step in case[key]:
                        self.send([step["send"]], expect)
                elif key == "generate":
                    spec = case[key]
                    items = []
                    for i in range(spec["count"]):
                        change = {}
                        target = change
                        *parents, name = spec["unique"].split(".")
                        for parent in parents:
                            target = target.setdefault(parent, {})
                        target[name] = f"golden-{i:06}"
                        items.append({**spec, "mutate": merge(spec.get("mutate", {}), change)})
                    self.send(items, expect, within=spec["within_seconds"])
                else:
                    self.send(case[key] if key == "send_batch" else [case[key]], expect)
            if "request" in case:
                self.request(case, remove_transform=False)
            if "do" in case:
                self.operate(case["do"])
            then = case.get("then")
            if isinstance(then, dict):
                self.before_outage = {str(n["id"]): n for n in self.notifications()}
                require(self.before_outage, "No notifications observable during the outage")
                time.sleep(then["wait_seconds"])
                self.set_mode(then["fake_telecom_mode"])
            if verify:
                delivery_wait = then.get("wait_until_delivered_seconds") if isinstance(then, dict) else None
                with checks.measure():
                    self.check_all(expect, timeout=delivery_wait)
            elif "action_run" in expect:
                self.check("action_run", expect["action_run"])
            elif "alert" in expect:
                self.check("alert", {k: v for k, v in expect["alert"].items() if k == "rule"})
            if isinstance(then, list):
                with checks.measure("then"):
                    for step in then:
                        self.send([step["send"]], case["then_expect"])
                    if verify:
                        self.check_all(case["then_expect"])
            if verify and "without_transform_expect" in case:
                with checks.measure("without transform"):
                    self.request(case, remove_transform=True)
                    self.check_all(case["without_transform_expect"])
            if verify and expect.get("worker_still_running"):
                sample = deepcopy(case["send_batch"][0])
                probe_id = "golden-worker-probe-" + uuid4().hex
                sample.update(amqp_message_id=probe_id, headers={"Idempotency-Key": probe_id})
                self.send([sample], {"event": {"status": "processed"}})
        except (Mismatch, Unavailable) as exc:
            checks.errors.append(exc)
        finally:
            if before_mode is not None:
                with checks.measure("restore fake-telecom mode"):
                    self.set_mode(before_mode)
        checks.finish()

    def set_mode(self, mode):
        reply = self.telecom("POST", "/mode", json={"mode": mode})
        equal(reply.get("mode"), mode, "fake-telecom mode")

    def send(self, specs, expect, within=None):
        source = self.r.sources[specs[0]["source"]]
        require(all(s["source"] == source["id"] for s in specs), "Batch mixes input transports")
        transport = source["transport"]
        ids = set()
        if transport == "logfile":
            target = self.r.log_input
            if target is None or not target.is_file() or target.resolve().is_relative_to(ROOT / "fixtures"):
                raise Unavailable("Set --log-input to an existing file watched by the target stack's log tailer")
            self.r.page("/v1/events", params={"source": source["id"]})
            content = (ROOT / specs[0]["file"]).read_bytes()
            count = len(content.splitlines())
            try:
                # O_APPEND without O_CREAT keeps an absent/mistyped input from becoming a fixture file.
                with os.fdopen(os.open(target, os.O_RDWR | os.O_APPEND), "r+b") as logfile:
                    if logfile.seek(0, os.SEEK_END):
                        logfile.seek(-1, os.SEEK_END)
                        if logfile.read(1) != b"\n":
                            raise Unavailable("--log-input ends in a partial line; use a complete watched log")
                    logfile.write(content if content.endswith(b"\n") else content + b"\n")
            except OSError as exc:
                raise Unavailable(f"Cannot append to --log-input ({type(exc).__name__})") from exc
        else:
            messages = []
            for s in specs:
                headers = {"X-Demo-Clock": self.clock, "X-Gen-Sent-At": str(time.time_ns() // 1_000_000),
                           **s.get("headers", {})}
                body = payload(s)
                if "trace" in s:
                    headers["X-Demo-Fault"] = s["trace"]["demo_fault"]
                    body = merge(body, {"trace": s["trace"]})
                messages.extend([(body, headers, s.get("amqp_message_id"))] * s.get("repeat", 1))
            count = len({mid for _, _, mid in messages if mid}) + sum(mid is None for _, _, mid in messages)
            if transport == "rabbitmq":
                if not self.r.broker:
                    raise Unavailable("RabbitMQ broker publishing disabled (enable with --broker)")
                self.r.page("/v1/events", params={"source": source["id"]})
                started = time.monotonic()
                asyncio.run(publish(source["queue"], messages))
            elif transport == "http":
                started = time.monotonic()
                for body, headers, key in messages:
                    if key:
                        headers["Idempotency-Key"] = key
                    status, data = self.r.http("POST", "/v1/events/" + source["id"], status_only=True,
                                               headers={**headers, "X-Api-Key": source["api_key"]}, json=body)
                    equal(status, 202, "HTTP ingest status")
                    equal(data.get("rejected", []), [], "ingest rejections")
                    equal(data.get("conflicts", 0), 0, "ingest conflicts")
                    for field in ("accepted", "duplicates"):
                        self.ingest[field] += data[field]
                    ids.update(data["event_ids"])
                equal(len(ids), count, "distinct returned event ids")
            else:
                raise Unavailable(f"Unsupported input transport {transport}")
            if within is not None:
                require(time.monotonic() - started <= within, f"Generation exceeded {within}s")
        if transport != "http":
            fresh = set()
            def discover():
                nonlocal fresh
                found = {e["id"] for e in self.r.page("/v1/events", params={"source": source["id"]})}
                fresh = found - self.seen_ids
                equal(len(fresh), count, f"new persisted {source['id']} events")
                return fresh
            try:
                ids = wait_for(discover, self.r.timeout)
            except Mismatch as exc:
                if transport == "logfile" and not fresh and "new persisted" in str(exc):
                    raise Unavailable("No events observed from --log-input; verify the target stack's log tailer") from exc
                raise
        explains = {}
        def processed():
            for event_id in sorted(ids - set(explains)):
                item = self.api(f"/events/{event_id}/explain")
                status = item.get("status")
                require(status in TERMINAL, f"Event {event_id} remained {status!r} at processing deadline")
                outcome = item.get("outcome")
                require(outcome and outcome not in {"ACCEPTED", "DUPLICATE", "CONFLICT", "PENDING", "PROCESSING"},
                        f"Event {event_id} is {status!r}, with no terminal explain outcome ({outcome!r})")
                explains[event_id] = item
            return explains
        self.explains = wait_for(processed, self.r.timeout)
        if "events" not in expect:
            expected_status = expect.get("event", {}).get("status", "processed")
            for event_id, e in self.explains.items():
                equal(e["status"], expected_status, f"event {event_id} status")
        self.seen_ids.update(ids)

    def request(self, case, remove_transform):
        body = deepcopy(case["body"])
        if "samples" in body:
            for s in body["samples"]:
                s["event"] = payload({"file": s.pop("event_file"), "mutate": s.pop("mutate", {})})
            self.checked_samples = body["samples"]
        if remove_transform:
            body["schema"].pop("transforms")
        spec = case["request"]
        self.response = self.r.http(spec["method"], spec["path"], json=body)

    def snapshot_receivers(self, case):
        expect, operation = case["expect"], case.get("do", {})
        channels = {n["channel"] for n in expect.get("notifications", [])}
        channels.update(pair.split("/")[1] for pair in expect.get("notifications_count", {}))
        channels.update(ch for group in expect.get("not_notified_channels", {}).values() for ch in group)
        if "not_notified" in expect or "notifications_channels_only" in expect or "reconciliation" in expect:
            channels.update(("email", "phone", "sms"))
        if "inbound_sms" in operation or "inbound_sms_wrong_code_times" in operation:
            channels.add("sms")
        receivers = ({"telecom"} if channels & {"phone", "sms"} else set()) | ({"mail"} if "email" in channels else set())
        for receiver in sorted(receivers):
            try:
                if receiver == "mail":
                    baseline = {m["ID"] for m in self.mail_list()}
                else:
                    baseline = {str(m["idempotency_key"]) for m in rows(self.telecom("GET", "/messages"))}
                self.receivers_before[receiver] = baseline
            except (Unavailable, Mismatch) as exc:
                # Keep independent API assertions usable when a receiver is offline.
                self.receivers_before[receiver] = exc

    def mail_list(self):
        return self.r.page("/api/v1/messages", base=self.r.mailpit, key="messages", offset_key="start")

    def receipts(self, channel):
        receiver = "mail" if channel == "email" else "telecom"
        baseline = self.receivers_before[receiver]
        if isinstance(baseline, Exception):
            raise baseline
        if receiver == "telecom":
            return [m for m in rows(self.telecom("GET", "/messages")) if m["channel"] == channel
                    and str(m["idempotency_key"]) not in baseline]
        found = []
        for m in self.mail_list():
            if m["ID"] in baseline:
                continue
            headers = self.mail(f"/api/v1/message/{quote(m['ID'], safe='')}/headers")
            ids = next((v for k, v in headers.items() if k.lower() == "x-notification-id"), [])
            for recipient in m["To"]:
                found.append({"idempotency_key": ids[0] if len(ids) == 1 else None,
                              "channel": "email", "to": recipient["Address"]})
        return found

    def approval_code(self, sender):
        def read():
            ids = {str(n["id"]) for n in self.notifications() if n["kind"] == "approval_request"}
            for message in self.receipts("sms"):
                if str(message["idempotency_key"]) in ids and message["to"] == sender:
                    code = re.search(r"\bYES\s+(\d{6})\b", message["text"], re.I)
                    if code:
                        return code[1]
            raise Mismatch("No fresh approval SMS with the six-digit code reached the named sender")
        return wait_for(read, self.r.timeout)

    def operate(self, op):
        if "execute_step" in op:
            alert_id = self.one_alert()["id"]
            self.http_status, self.action = self.r.http("POST", f"/v1/alerts/{alert_id}/actions",
                json={"step_index": op["execute_step"]}, headers={"X-User": op["actor"]}, status_only=True)
        elif "approve" in op:
            require("id" in self.action, "No previous action run to approve")
            self.http_status, self.action = self.r.http("POST", f"/v1/action-runs/{self.action['id']}/approve",
                headers={"X-User": op["actor"]}, status_only=True)
        elif "inbound_sms" in op or "inbound_sms_wrong_code_times" in op:
            headers = op.get("headers", {}).copy()
            if op.get("with_secret"):
                if not (secret := os.getenv("INBOUND_SECRET")):
                    raise Unavailable("INBOUND_SECRET is required for authenticated SMS")
                headers["X-Provider-Secret"] = secret
            sender = op.get("from") or op["inbound_sms"]["from"]
            code = self.approval_code(sender)
            if "inbound_sms_wrong_code_times" in op:
                body = f"YES {(int(code) + 1) % 1_000_000:06d}"
                attempts = op["inbound_sms_wrong_code_times"] + 1
            else:
                body = op["inbound_sms"]["body"].replace("<code>", code)
                attempts = 1
            for index in range(attempts):
                self.http_status, _ = self.r.http("POST", "/v1/inbound/sms", headers=headers,
                    json={"from": sender, "body": body}, status_only=True)
                if index < attempts - 1:
                    require(self.http_status in (403, 404, 409, 422, 429),
                            f"Wrong-code attempt {index + 1} returned HTTP {self.http_status}")
        else:
            raise Mismatch(f"Unknown action {list(op)}")

    def check_all(self, expect, timeout=None):
        checks = Checks()
        delivery_deadline = time.monotonic() + (self.r.timeout if timeout is None else timeout)
        # The pricing policy sends on its configured digest interval, after the inbox is processed.
        if "notifications_channels_only" in expect and self.r.timeout > 0:
            rule_ids = {a["rule"] for a in expect.get("alerts", [])}
            for rule in self.r.rules:
                group = rule.get("group", {})
                if rule["id"] in rule_ids and group.get("mode") == "digest":
                    period = re.fullmatch(r"(\d+)([smh])", group["every"])
                    if period:
                        delivery_deadline += int(period[1]) * {"s": 1, "m": 60, "h": 3600}[period[2]]
        for key, want in expect.items():
            with checks.measure(key):
                if key in {"notifications", "notifications_count", "not_notified", "not_notified_channels",
                           "notifications_channels_only", "reconciliation"}:
                    wait_for(lambda: self.delivery_check(key, want), max(0, delivery_deadline - time.monotonic()))
                else:
                    self.check(key, want)
        checks.finish()

    def delivered(self, row):
        equal(row["state"], "delivered", f"{row['recipient']}/{row['channel']} state")
        if row["channel"] == "ui":
            return
        contact = self.r.contacts.get(row["recipient"], {}).get(row["channel"])
        require(contact is not None, f"No configured {row['channel']} contact for {row['recipient']}")
        matches = [m for m in self.receipts(row["channel"]) if str(m["idempotency_key"]) == str(row["id"])]
        equal([m["to"] for m in matches], [contact], f"receiver destinations for notification {row['id']}")

    def expected_notice(self, notices, item):
        who, channel = item["recipient"], item["channel"]
        unknown = item.keys() - {"recipient", "channel", "state", "attempts_gte"}
        require(not unknown, f"Unsupported notification checks {sorted(unknown)}")
        matching = [n for n in notices if (n["recipient"], n["channel"]) == (who, channel)]
        require(matching, f"No {channel} notification for {who}")
        eligible = [n for n in matching if n.get("state") == item.get("state", "delivered")
                    and n.get("attempts", 0) >= item.get("attempts_gte", 0)]
        if not eligible and channel != "ui":
            baseline = self.receivers_before["mail" if channel == "email" else "telecom"]
            if isinstance(baseline, Unavailable) and all(n["state"] in ("pending", "sending", "retrying") for n in matching):
                raise Unavailable(f"{who}/{channel} still pending; {baseline}")
        require(eligible, f"{who}/{channel}: expected {item!r}, observed "
                + str([(n.get("state"), n.get("attempts")) for n in matching]))
        for n in eligible:
            self.delivered(n)

    def delivery_check(self, key, want):
        notices = self.notifications()
        if key == "notifications":
            checks = Checks()
            for item in want:
                with checks.measure():
                    self.expected_notice(notices, item)
            checks.finish()
        elif key in ("not_notified", "not_notified_channels"):
            forbidden = ({who: ["email", "sms", "phone", "ui"] for who in want}
                         if key == "not_notified" else want)
            for who, channels in forbidden.items():
                actual = [n["channel"] for n in notices if n["recipient"] == who and n["channel"] in channels]
                equal(actual, [], f"forbidden channels for {who}")
                for channel in channels:
                    contact = self.r.contacts.get(who, {}).get(channel)
                    if contact and channel != "ui":
                        require(not any(m["to"] == contact for m in self.receipts(channel)),
                                f"Receiver recorded forbidden {channel} for {who}")
        elif key == "notifications_count":
            for pair, count in want.items():
                who, channel = pair.split("/")
                matching = [n for n in notices if (n["recipient"], n["channel"]) == (who, channel)]
                equal(len(matching), count, f"logical notifications for {pair}")
                for n in matching:
                    self.delivered(n)
                if channel != "ui":
                    contact = self.r.contacts[who][channel]
                    equal(sum(m["to"] == contact for m in self.receipts(channel)), count, f"receiver count for {pair}")
        elif key == "notifications_channels_only":
            equal({(n["recipient"], n["channel"]) for n in notices},
                  {(who, ch) for who, channels in want.items() for ch in channels}, "recipient/channel set")
            for n in notices:
                self.delivered(n)
            allowed = {n["channel"] for n in notices}
            for channel in sorted({"email", "sms", "phone"} - allowed):
                equal(len(self.receipts(channel)), 0, f"unexpected {channel} receipts")
        elif key == "reconciliation":
            final = {str(n["id"]): n for n in notices}
            lost = []
            for notification_id in self.before_outage:
                if notification_id not in final:
                    lost.append(notification_id)
                    continue
                try:
                    for field in ("recipient", "channel"):
                        equal(final[notification_id][field], self.before_outage[notification_id][field], field)
                    self.delivered(final[notification_id])
                except Mismatch:
                    lost.append(notification_id)
            equal(len(lost), want["lost"], f"lost of {len(self.before_outage)} pre-outage notifications {lost}")

    def check(self, key, want):
        if key in ("alert", "alerts"):
            all_alerts = self.alerts()
            specs = want if key == "alerts" else [want]
            for spec in specs:
                selected = [a for a in all_alerts if ("rule" not in spec or a["rule_id"] == spec["rule"])
                            and all(a.get("group_values", {}).get(k) == v for k, v in spec.get("group", {}).items())]
                equal(len(selected), 1, f"alert matches for {spec}")
                self.check_alert(selected[0], spec)
            if key == "alerts":
                rules = {s["rule"] for s in specs}
                equal(sum(a["rule_id"] in rules for a in all_alerts), len(specs), "alerts for expected rules")
        elif key == "no_alert_for_rule":
            equal([a["id"] for a in self.alerts() if a["rule_id"] == want], [], f"alerts for {want}")
        elif key == "open_alerts_for_rule":
            actual = Counter(a["rule_id"] for a in self.alerts() if a["status"] == "open")
            for rule, n in want.items():
                equal(actual[rule], n, f"open alerts for {rule}")
        elif key == "no_alert_for":
            for group in want:
                require(not any(group.items() <= a["group_values"].items() for a in self.alerts()),
                        f"Unexpected alert for {group}")
        elif key == "alert_runbook":
            observed = [a["runbook_id"] for a in self.alerts()]
            require(observed and all(r == want for r in observed), f"Expected runbook {want}, observed {observed}")
        elif key == "runbook_steps":
            actual = rows(self.api(f"/alerts/{self.one_alert()['id']}/runbook"), "steps")
            for spec in want:
                matches = [s for s in actual if s["index"] == spec["index"]]
                equal(len(matches), 1, f"runbook step {spec['index']}")
                subset(matches[0], spec, "step")
        elif key == "event":
            equal(len(self.explains), 1, "event count")
            e = self.api(f"/events/{next(iter(self.explains))}")
            subset(e, {("schema_name" if k == "schema" else k): v for k, v in want.items()}, "event")
        elif key == "events":
            events = [self.api(f"/events/{i}") for i in self.explains]
            equal(len(events), len(want), "number of distinct batch events")
            for identity, status in want.items():
                matches = [e for e in events if identity in e["fields"].values()]
                equal([e["status"] for e in matches], [status], f"event {identity}")
        elif key == "worker_still_running":
            equal(want, True, "worker probe requested")  # perform sends a fresh, independently identified probe last.
        elif key == "excluded_lines":
            events = [self.api(f"/events/{i}") for i in self.explains]
            for token in want:
                matches = [e for e in events if token in e["fields"].values()]
                require(matches, f"Excluded log input {token!r} was never ingested")
                for e in matches:
                    explain = self.explains[e["id"]]
                    require(explain["outcome"] in {"NO_MATCH", "UNROUTED"}
                            and not any(d.get("alert_id") for d in explain["decisions"]),
                            f"Excluded log input {token!r} contributed to an alert")
        elif key == "last_decision":
            equal(len(self.explains), 1, "number of events in last step")
            equal(next(iter(self.explains.values()))["outcome"], want, "last event decision")
        elif key == "schema_issue":
            issues = rows(self.api("/schema-issues"))
            matched = [i for i in issues if i.get("sample_event_id") in self.explains]
            if not matched and len(issues) == 1 and "sample_event_id" not in issues[0]:
                matched = issues  # Older public shape has one issue after reset and no sample id.
            equal(bool(matched), want["listed"], "issue for this input")
            for field in want.get("sample_fields_include", []):
                require(any(field in i["sample_fields"] for i in matched), f"Missing issue sample field {field}")
        elif key == "ingest":
            def check_counts():
                actual = self.ingest
                if self.funnel_before is not None:
                    now = self.api("/stats/funnel")
                    actual = {k: now[k] - self.funnel_before[k] for k in ("accepted", "duplicates")}
                for field, n in want.items():
                    equal(actual["duplicates" if field == "duplicate" else field], n, f"ingest {field}")
            wait_for(check_counts, self.r.timeout)
        elif key in ("http_status", "sixth_attempt_http_status"):
            equal(self.http_status, want, key)
        elif key == "action_run":
            require(self.http_status is not None and 200 <= self.http_status < 300,
                    f"Action returned HTTP {self.http_status}")
            subset(self.action, want, key)
        elif key == "audit_contains":
            audit_rows = [t for t in self.api(f"/alerts/{self.one_alert()['id']}")["timeline"] if t["kind"] == "audit"]
            for entry in want:
                def matches(row):
                    actor, _, action = row.get("text", "").partition(" ")
                    public = {"actor": row.get("actor", actor), "action": row.get("action", action)}
                    return all(public.get(k) == v for k, v in entry.items())
                require(any(matches(row) for row in audit_rows), f"Public audit timeline omits {entry}")
        elif key in ("violations", "violations_gte", "scenarios_checked_gte", "violation_problem_contains"):
            if key == "violation_problem_contains":
                require(any(want in v["problem"] for v in rows(self.response, "violations")),
                        f"No reported violation contains {want!r}")
            else:
                count = len(rows(self.response, key.removesuffix("_gte")))
                if key.endswith("_gte"):
                    require(count >= want, f"Expected at least {want}, observed {count}")
                else:
                    equal(count, want, key)
        elif key in ("all_pass", "deciding_fields_include", "sample_0", "unit_warning"):
            self.check_samples(key, want)
        else:
            raise Mismatch(f"Unsupported golden expectation {key}")

    def check_alert(self, alert, spec):
        aliases = {"rule": "rule_id", "team": "team_id", "runbook": "runbook_id", "group": "group_values",
                   "title_equals": "title"}
        for field, want in spec.items():
            if field == "title_contains":
                for text in want:
                    require(text in alert["title"], f"Missing {text!r} in title {alert['title']!r}")
            elif field == "fields_shown":
                for name in want:
                    require(name in alert["fields"], f"Alert omits field {name!r}")
            elif field == "occurrence_count_delta":
                equal(alert["occurrence_count"] - self.before_counts.get(alert["id"], 0), want, field)
            else:
                subset(alert, {aliases.get(field, field): want}, "alert")

    def check_samples(self, key, want):
        results = rows(self.response, "samples" if "samples" in self.response else "results")
        equal(len(results), len(self.checked_samples), "sample result count")
        def passes(sample):
            value = next((sample[k] for k in ("pass", "passed", "status") if k in sample), None)
            require(type(value) is bool or value in ("pass", "fail"), f"Missing sample pass/fail result: {value}")
            return value is True or value == "pass"
        if key == "all_pass":
            equal(all(passes(s) for s in results), want, key)
            if key in self.response:
                equal(self.response[key], want, "aggregate all_pass")
            for i, (sample, expected) in enumerate(zip(results, self.checked_samples)):
                actual = sample.get("actual", sample)
                route = expected["expect"]
                for field in ("recipients", "channels"):
                    equal(sorted(actual[field]), [] if route == "quiet" else sorted(route[field]), f"sample {i} {field}")
        elif key == "deciding_fields_include":
            fields = [f for s in results for f in s.get("deciding_fields", [])]
            for field in want:
                require(field in fields, f"Missing deciding field {field!r}, observed {fields}")
        elif key == "sample_0":
            equal("pass" if passes(results[0]) else "fail", want, key)
        else:
            warnings = self.response.get("unit_warnings", []) + [w for s in results for w in s.get("unit_warnings", [])]
            path = re.compile(rf"(?<![\w.]){re.escape(want)}(?![\w.])")
            require(any(path.search(str(w)) for w in warnings), f"No unit warning for {want!r}")


def table(results):
    def cell(value):
        return " ".join(value.splitlines()).replace("|", r"\|")
    lines = ["| Case | Status | Evidence |", "|---|---|---|"]
    lines.extend(f"| {cell(r.case_id)} | {r.status} | {cell(r.detail)} |" for r in results)
    return "\n".join(lines)


def replace_report(previous, report):
    section = "## Golden run\n\n" + report.rstrip() + "\n"
    match = re.search(r"^## Golden run[ \t]*\r?\n.*?(?=^## |\Z)", previous, re.M | re.S)
    if match:
        suffix = previous[match.end():]
        return previous[:match.start()] + section + ("\n" + suffix if suffix else "")
    return previous + ("\n" if previous.endswith("\n") else "\n\n") + section


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=os.getenv("ALERTING_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--telecom-url", default=os.getenv("FAKE_TELECOM_URL", "http://127.0.0.1:8100"))
    parser.add_argument("--mailpit-url", default=os.getenv("MAILPIT_URL", "http://127.0.0.1:8025"))
    parser.add_argument("--broker", action=argparse.BooleanOptionalAction, default=False, help="enable RabbitMQ input")
    parser.add_argument("--log-input", type=Path, help="existing disposable log file watched by the target stack")
    parser.add_argument("--timeout", type=float, default=10, help="seconds to wait for processing and delivery")
    parser.add_argument("--no-write", action="store_true", help="print the table without updating docs/GATES.md")
    args = parser.parse_args()
    if args.timeout < 0:
        parser.error("--timeout must be nonnegative")
    with httpx.Client(timeout=3, trust_env=False) as client:
        run = Runner(client, api=args.api_url, telecom=args.telecom_url, mailpit=args.mailpit_url,
                     timeout=args.timeout, broker=args.broker, log_input=args.log_input)
        results = run.run(yaml.safe_load(GOLDEN.read_text()))
    report = table(results)
    print(report)
    if not args.no_write:
        def display(url):
            return str(httpx.URL(url).copy_with(username=None, password=None, query=None, fragment=None))
        command = f"backend/.venv/bin/python loadtest/golden_runner.py --{'broker' if args.broker else 'no-broker'}"
        ran_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        evidence = (f"Run by `golden_runner.py` on {ran_at}. Command `{command}` with API `{display(args.api_url)}`, "
                    f"fake-telecom `{display(args.telecom_url)}` and Mailpit `{display(args.mailpit_url)}`.\n\n")
        before = GATES.read_text()
        updated = replace_report(before, evidence + report)
        if updated != before:
            GATES.write_text(updated)
    return 1 if any(r.status == "FAIL" for r in results) else 2 if any(r.status != "PASS" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
