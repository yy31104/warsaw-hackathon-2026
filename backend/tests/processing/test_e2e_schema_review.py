"""Publish a reviewed schema while an independently started production worker is polling."""

import asyncio
import os
import shutil
import sys
from types import SimpleNamespace

import pytest
import yaml

from alerting import db
from alerting.api.app import app
from alerting.config import files
from alerting.rules.storage import get_settings
from .test_settings_reload import wait_for


@pytest.mark.e2e
async def test_live_worker_resolves_with_published_schema(api, database, monkeypatch, tmp_path):
    monkeypatch.setitem(app.dependency_overrides, db.pool, lambda: database)
    directory = files.directory()
    fallback = next(t for t in directory["teams"] if t["id"] == directory["fallback_team"])
    actor = fallback["members"][0]
    source = next(
        s for s in files.sources() if s["transport"] == "http" and not s.get("split_path")
    )
    config = tmp_path / "config"
    shutil.copytree(files.CONFIG_DIR, config)
    rule = {
        "id": "reviewed-episode",
        "owner": "team:" + fallback["id"],
        "match": {"alarm_state": "down"},
        "route": [{"to": "team:" + fallback["id"], "channels": ["ui"]}],
    }
    (config / "rules.yaml").write_text(yaml.safe_dump({"rules": [rule]}))
    schema = {
        "id": "vendor-health",
        "name": "vendor.health",
        "detect": {"probe": "vendor"},
        "alert_key": "{{device}}",
        "resolve_when": {"alarm_state": "up"},
    }
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alerting.rules.worker",
        "--lane",
        "normal",
        env={**os.environ, "CONFIG_DIR": str(config), "DATABASE_URL": database.conninfo},
    )

    async def query(statement, params=()):
        async with database.connection() as conn:
            return await (await conn.execute(statement, params)).fetchone()

    async def send(status):
        result = await api.post(
            "/v1/events/" + source["id"],
            headers={"X-Api-Key": source["api_key"]},
            json={"device": "router-17", "probe": "vendor", "alarm_state": status},
        )
        assert result.status_code == 202, result.text
        return result.json()["event_ids"][0]

    try:
        await wait_for(
            lambda: query(
                "select 1 from backend_file_rule_revisions where rule_id=%s", (rule["id"],)
            )
        )
        result = await api.post("/v1/schemas", json=schema, headers={"X-User": actor})
        assert result.status_code == 200, result.text
        first = await send("down")
        await wait_for(
            lambda: query("select 1 from events where id=%s and status='processed'", (first,))
        )
        assert await query("select status from alerts where rule_id=%s", (rule["id"],)) == ("open",)
        second = await send("up")
        await wait_for(
            lambda: query("select 1 from events where id=%s and status='processed'", (second,))
        )
        assert await query(
            "select alert_key,status,resolved_by from alerts where rule_id=%s", (rule["id"],)
        ) == ("router-17", "resolved", "system")
        assert worker.returncode is None
    finally:
        if worker.returncode is None:
            worker.terminate()
        await worker.wait()


async def test_schema_only_registry_keeps_file_rules(api, database, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, db.pool, lambda: database)
    directory = files.directory()
    actor = next(
        t["members"][0] for t in directory["teams"] if t["id"] == directory["fallback_team"]
    )
    schema = {"id": "schema-only", "name": "schema-only", "detect": {"probe": "vendor"}}
    published = await api.post("/v1/schemas", json=schema, headers={"X-User": actor})
    assert published.status_code == 200, published.text
    loader = SimpleNamespace(
        rules=lambda: [], schemas=lambda: [schema], directory=lambda: {}, runbooks=lambda: []
    )
    monkeypatch.setitem(sys.modules, "alerting.config.loader", loader)
    settings = await get_settings(database)
    assert settings.ruleset.schemas[schema["id"]]["origin"] == "ui"
    assert {rule["id"] for rule, _ in settings.ruleset.rules} == {
        rule["id"] for rule in files.rules() if rule.get("enabled", True)
    }
