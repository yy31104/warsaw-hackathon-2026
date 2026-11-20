"""Live smoke. Set CANARY_LIVE_GATEWAY_ROOT to a checkout with mocks/fake_telecom/app.py."""
import asyncio
import os
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from alerting.config import files

from alerting.canary.monitor import CANARY_NUMBER, OPS_NUMBER
from tests.processing.conftest import database  # noqa: F401 -- isolated PostgreSQL schema


ROOT = Path(__file__).resolve().parents[3]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


async def wait_for(client, url, predicate, processes):
    async with asyncio.timeout(15):
        while True:
            for name, process in processes:
                assert process.poll() is None, f"{name} exited with status {process.returncode}"
            try:
                response = await client.get(url)
                response.raise_for_status()
                if predicate(data := response.json()):
                    return data
            except httpx.TransportError:
                pass
            await asyncio.sleep(.1)


@pytest.mark.skipif(not os.getenv("CANARY_LIVE_GATEWAY_ROOT"), reason="Set CANARY_LIVE_GATEWAY_ROOT for live sockets")
async def test_worker_outage_reaches_direct_simulated_gateway(database, tmp_path):
    gateway_root = Path(os.environ["CANARY_LIVE_GATEWAY_ROOT"]).resolve()
    assert (gateway_root / "mocks/fake_telecom/app.py").is_file()
    source, = (s for s in files.sources() if s["id"] == "canary")

    with ExitStack() as stack:
        def reserve_port():
            sock = stack.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"

        api_sock, api = reserve_port()
        gateway_sock, gateway = reserve_port()
        env = dict(
            os.environ, DATABASE_URL=database.conninfo, CONFIG_DIR=str(files.CONFIG_DIR), DEMO_MODE="1",
            DB_ON_STARTUP="1", ASSIST_ENABLED="0", ALERTING_API_URL=api, FAKE_TELECOM_URL=gateway,
            FAKE_TELECOM_DB=str(tmp_path / "receipts.sqlite3"), CANARY_HOST="127.0.0.1",
            CANARY_API_KEY=source["api_key"], DELIVERY_TARGET_S="2",
        )

        def start(name, *args, sock=None):
            with (tmp_path / (name + ".log")).open("w") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", *args], cwd=ROOT / "backend", env=env,
                    stdout=log, stderr=subprocess.STDOUT, pass_fds=(sock.fileno(),) if sock else (),
                )
            stack.callback(stop, proc)
            if sock:
                sock.close()
            return name, proc

        def start_canary(name):
            sock, base = reserve_port()
            env["CANARY_PORT"] = str(sock.getsockname()[1])
            sock.close()
            return start(name, "alerting.canary"), base

        services = [
            start("api", "uvicorn", "alerting.api.app:app", "--fd", str(api_sock.fileno()), sock=api_sock),
            start("gateway", "uvicorn", "--app-dir", str(gateway_root), "mocks.fake_telecom.app:app",
                  "--fd", str(gateway_sock.fileno()), sock=gateway_sock),
            start("rules", "alerting.rules.worker", "--lane", "normal"),
        ]
        delivery = start("delivery", "alerting.delivery.worker", "--channels", "sms")
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            await wait_for(client, api + "/health", lambda d: d.get("status") == "ok", services)
            await wait_for(client, gateway + "/mode", lambda d: d.get("mode") == "ok", services)
            healthy_process, base = start_canary("monitor-healthy")
            healthy = await wait_for(
                client, base + "/status", lambda d: d["delivered"] == 1,
                [*services, delivery, healthy_process],
            )
            receipt, = (await client.get(gateway + "/messages", params={"to": CANARY_NUMBER})).json()["items"]
            assert healthy["last_probe"]["id"] in receipt["text"]
            assert healthy["last_e2e_latency_ms"] == receipt["received_at"] - receipt["gen_sent_at"]

            stop(delivery[1])
            stop(healthy_process[1])
            # Restart only the monitor to send the next scheduled pulse immediately.
            failed_process, base = start_canary("monitor-missed")
            missed = await wait_for(
                client, base + "/status", lambda d: (d["last_probe"] or {}).get("bypass") == "sent",
                [*services, failed_process],
            )
            assert missed["missed"] == 1 and missed["last_probe"]["outcome"] == "missed"
            probe_id = missed["last_probe"]["id"]
            alarm, = (await client.get(gateway + "/messages", params={"to": OPS_NUMBER})).json()["items"]
            assert alarm["idempotency_key"] == f"canary-alarm-{probe_id}" and probe_id in alarm["text"]
            assert f"canary missed id={probe_id}" in (tmp_path / "monitor-missed.log").read_text()

        async with database.connection() as conn:
            rows = await (await conn.execute(
                "select kind, contact, state from notifications order by created_at"
            )).fetchall()
        assert rows == [("canary", CANARY_NUMBER, "delivered"), ("canary", CANARY_NUMBER, "pending")]
