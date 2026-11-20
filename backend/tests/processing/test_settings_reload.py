import asyncio
import os
import shutil
import sys
from types import SimpleNamespace

import yaml
from psycopg.types.json import Jsonb

from alerting.config import files
from alerting.rules.storage import get_settings


async def wait_for(query):
    async with asyncio.timeout(12):
        while not (value := await query()):
            await asyncio.sleep(0.05)
        return value


async def test_running_worker_reloads_file(database, tmp_path):
    config = tmp_path / "config"
    shutil.copytree(files.CONFIG_DIR, config)
    team = files.directory()["teams"][0]["id"]
    rule = {
        "id": "amount-limit",
        "owner": f"team:{team}",
        "group": {"by": ["batch"]},
        "route": [{"to": f"team:{team}", "channels": ["ui"]}],
        "match": {"field": "amount", "op": "gt", "value": 100},
    }
    path = config / "rules.yaml"

    def write_rule():
        staged = config / "rules.tmp"
        staged.write_text(yaml.safe_dump({"rules": [rule]}))
        staged.replace(path)

    write_rule()

    async def send(batch):
        async with database.connection() as conn:
            return (
                await (
                    await conn.execute(
                        """insert into events(source_id,transport,payload_hash,raw,fields)
                values ('test','http','x','{}',%s) returning id""",
                        (Jsonb({"amount": 50, "batch": batch}),),
                    )
                ).fetchone()
            )[0]

    async def decision(event):
        async with database.connection() as conn:
            return await (
                await conn.execute(
                    "select outcome,rule_version from decisions where event_id=%s and rule_id=%s",
                    (event, rule["id"]),
                )
            ).fetchone()

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alerting.rules.worker",
        "--lane",
        "normal",
        env={**os.environ, "CONFIG_DIR": str(config), "DATABASE_URL": database.conninfo},
    )
    try:
        first = await send("before")
        assert await wait_for(lambda: decision(first)) == ("NO_MATCH", 1)
        rule["match"]["value"] = 25
        write_rule()

        async def new_version():
            async with database.connection() as conn:
                return await (
                    await conn.execute(
                        "select 1 from backend_file_rule_revisions where rule_id=%s and version=2",
                        (rule["id"],),
                    )
                ).fetchone()

        await wait_for(new_version)
        second = await send("after")
        assert await wait_for(lambda: decision(second)) == ("NEW_ALERT", 2)
        assert proc.returncode is None
        async with database.connection() as conn:
            assert await (await conn.execute("select rule_version from alerts")).fetchone() == (2,)
            assert await (
                await conn.execute("select recipient,channel from notifications")
            ).fetchone() == (f"team:{team}", "ui")
    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()


async def test_config_notification_switches_to_populated_loader(database, monkeypatch):
    directory = files.directory()
    rule = {"id": "runtime", "match": {}, "version": 7}
    loaded = []

    async def rules():
        return list(loaded)

    loader = SimpleNamespace(
        rules=rules,
        schemas=lambda: [],
        directory=lambda: directory if loaded else {},
        runbooks=lambda: [],
    )
    monkeypatch.setitem(sys.modules, "alerting.config.loader", loader)
    first = await get_settings(database)
    assert all(r["id"] != rule["id"] for r, _ in first.ruleset.rules)
    loaded.append(rule)

    async def signal_and_read():
        async with database.connection() as conn:
            await conn.execute(
                "select pg_notify('config',%s)", ('{"kind":"rule","id":"runtime","version":7}',)
            )
        snapshot = await get_settings(database)
        return next((r for r, _ in snapshot.ruleset.rules if r["id"] == rule["id"]), None)

    assert (await wait_for(signal_and_read))["version"] == 7
    # No file edit and no reconnect. A second publication must invalidate the same provider.
    loaded[0] = {**rule, "version": 8}

    async def version_eight():
        return (await signal_and_read() or {}).get("version") == 8

    await wait_for(version_eight)
