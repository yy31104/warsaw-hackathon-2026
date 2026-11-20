import asyncio
import json
import os
import runpy
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import aio_pika
import pytest

from alerting.clock import SYSTEM
from alerting.config import files
from alerting.ingest.amqp import store_and_ack

ROOT = Path(__file__).resolve().parents[3]
event_bodies = runpy.run_path(str(ROOT / "tools/publish.py"))["event_bodies"]
SAMPLE_PATH = ROOT / "fixtures/events/workflow_failed.json"
SAMPLE = SAMPLE_PATH.read_bytes()


def test_retransmissions_preserve_the_original_bytes():
    assert list(event_bodies(SAMPLE, 2)) == [SAMPLE, SAMPLE]


def test_unique_changes_only_the_requested_field():
    original = json.loads(SAMPLE)
    first, second = (json.loads(body) for body in event_bodies(SAMPLE, 2, unique="workflowId"))
    assert first["workflowId"] != second["workflowId"]
    for event in (first, second):
        assert event["workflowId"].startswith(original["workflowId"] + "-")
        assert {**event, "workflowId": original["workflowId"]} == original


def test_invalid_sample_or_field_is_rejected_before_publishing():
    with pytest.raises(ValueError, match="JSON object"):
        event_bodies(b"[]", 1)
    with pytest.raises(ValueError, match="top-level string field"):
        event_bodies(SAMPLE, 1, unique="error")


def test_http_cli_reports_duplicates_and_fails_on_an_ingest_conflict(tmp_path):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            key = self.headers["Idempotency-Key"]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            prior = next((r[-1] for r in received if r[1] == key), None)
            received.append((self.path, key, self.headers["X-Api-Key"], self.headers["X-Gen-Sent-At"], body))
            result = {"accepted": int(prior is None), "duplicates": int(prior == body),
                      "conflicts": int(prior is not None and prior != body), "rejected": []}
            self.send_response(202)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *args):
            pass

    source = {"id": "test-hook", "transport": "http", "api_key": "demo-key"}
    (tmp_path / "sources.yaml").write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = [sys.executable, "tools/publish.py", "--source", source["id"], "--file", str(SAMPLE_PATH),
                       "--count", "2", "--message-id", "repeat-42"]
            env = {**os.environ, "CONFIG_DIR": str(tmp_path),
                   "ALERTING_API_URL": f"http://127.0.0.1:{server.server_port}"}
            def run(*args):
                return subprocess.run([*command, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)

            ok = run("--via", "http")
            assert ok.returncode == 0, ok.stderr
            assert "accepted=1 duplicates=1" in ok.stdout
            assert len(received) == 2
            for path, key, api_key, sent, body in received:
                assert (path, key, api_key, body) == ("/v1/events/test-hook", "repeat-42", "demo-key", SAMPLE)
                assert int(sent) > 0
            conflict = run("--unique", "workflowId")
            assert conflict.returncode != 0 and "conflict" in conflict.stderr.lower()
            assert len(received) == 3
        finally:
            server.shutdown()
            thread.join()


async def test_cli_retransmission_stores_one_event(inbox_pool, tmp_path):
    url = os.getenv("INGEST_TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("set INGEST_TEST_RABBITMQ_URL to run the live publisher test")
    source = {"id": "workflow-bus", "transport": "rabbitmq", "queue": "test-publish-" + uuid4().hex}
    (tmp_path / "sources.yaml").write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    async with await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(source["queue"], durable=True)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "tools/publish.py", "--source", source["id"], "--file", str(SAMPLE_PATH),
                 "--message-id", "repeat-42", "--count", "2"],
                cwd=ROOT, env={**os.environ, "CONFIG_DIR": str(tmp_path), "RABBITMQ_URL": url},
                capture_output=True, text=True, timeout=20,
            )
            assert result.returncode == 0, result.stderr
            deliveries = []
            for _ in range(2):
                message = await queue.get(timeout=5)
                assert message.message_id == "repeat-42" and message.body == SAMPLE
                deliveries.append((source, message, SYSTEM.now()))
            await store_and_ack(deliveries, files.schemas(), inbox_pool)
            async with inbox_pool.connection() as db:
                events = await (await db.execute("SELECT id, source_event_id FROM events")).fetchall()
                audit = await (await db.execute("SELECT kind, existing_event_id FROM ingest_rejections")).fetchall()
            assert len(events) == 1 and events[0][1] == "repeat-42"
            assert audit == [("DUPLICATE", events[0][0])]
        finally:
            await queue.delete(if_unused=False, if_empty=False)
