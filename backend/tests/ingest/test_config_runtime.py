import asyncio
import shutil

import yaml
from psycopg.types.json import Jsonb

from alerting.config import files, loader
from alerting.rules.storage import get_settings
from alerting.rules.worker import process_batch


def rule(id="new-runtime-rule", threshold=100, owner="user:anna"):
    return {
        "id": id,
        "owner": owner,
        "match": {"field": "holdout.counter", "op": "gt", "value": threshold},
        "route": [{"to": "user:anna", "channels": ["ui"]}],
        "group": {"by": ["holdout.case"]},
        "present": {"title": "Runtime rule {{$severity}} {{holdout.case}}", "color": "blue"},
    }


async def send_event(db, key, counter=50):
    fields = {"holdout.counter": counter, "holdout.case": key, "$severity": "info"}
    async with db.connection() as conn:
        row = await conn.execute("""
            insert into events(source_id,transport,payload_hash,raw,fields,severity)
            values ('test','http','runtime-test','{}',%s,'info') returning id
        """, (Jsonb(fields),))
        return (await row.fetchone())[0]


async def decision(db, event_id, rule_id):
    async with db.connection() as conn:
        row = await conn.execute("""
            select outcome, rule_version from decisions where event_id=%s and rule_id=%s
            and outcome in ('NO_MATCH','NEW_ALERT')
        """, (event_id, rule_id))
        return await row.fetchone()


async def test_ui_rule_publishes_to_the_next_claimed_event(client, inbox_pool):
    actor = {"X-User": "anna"}
    saved = await client.post("/v1/rules", json=rule(), headers=actor)
    assert saved.status_code == 201, saved.text
    assert saved.json()["version"] == 1 and saved.json()["origin"] == "ui"
    before = await send_event(inbox_pool, "before")
    assert await process_batch(inbox_pool) == 1
    assert await decision(inbox_pool, before, rule()["id"]) == ("NO_MATCH", 1)

    replacement = {**rule(threshold=25), "version": 1, "source": "heuristic"}
    endpoint = f"/v1/rules/{rule()['id']}"
    update = await client.put(endpoint, json=replacement, headers=actor)
    assert update.status_code == 200 and update.json()["version"] == 2, update.text
    after = await send_event(inbox_pool, "after")
    assert await process_batch(inbox_pool) == 1
    assert await decision(inbox_pool, after, replacement["id"]) == ("NEW_ALERT", 2)
    async with inbox_pool.connection() as conn:
        result = await conn.execute("""
            select a.title,a.rule_version,n.recipient,n.channel from alerts a
            join notifications n on n.alert_id=a.id where a.rule_id=%s
        """, (replacement["id"],))
        assert await result.fetchall() == [("Runtime rule info after", 2, "anna", "ui")]
        result = await conn.execute("""
            select actor,detail from audit_log where action='rule.publish' and target_id=%s order by id desc
        """, (replacement["id"],))
        assert await result.fetchone() == ("anna", {"version": 2, "source": "heuristic"})


async def test_file_guard_owner_check_and_competing_versions(client, inbox_pool):
    anna = {"X-User": "anna"}
    lee = {"X-User": "lee"}
    file_rule = (await client.get("/v1/rules")).json()["items"][0]
    assert file_rule["origin"] == "file"
    assert (await client.put(f"/v1/rules/{file_rule['id']}", json=file_rule, headers=lee)).status_code == 409
    assert (await client.post("/v1/rules", json=file_rule, headers=lee)).status_code == 409

    response = await client.post("/v1/rules", json=rule(), headers=anna)
    current = response.json()
    path = f"/v1/rules/{current['id']}"
    assert (await client.put(path, json={**current, "owner": "user:lee"}, headers=lee)).status_code == 403
    assert (await client.put(path, json=current, headers=anna)).json()["version"] == 1
    first, second = await asyncio.gather(*(
        client.put(path, json={**rule(threshold=threshold), "version": 1}, headers=anna)
        for threshold in (20, 40)
    ))
    assert sorted(result.status_code for result in (first, second)) == [200, 409]
    async with inbox_pool.connection() as conn:
        result = await conn.execute("""
            select version,active,origin from config_objects where kind='rule' and id=%s order by version
        """, (current["id"],))
        assert await result.fetchall() == [(1, False, "ui"), (2, True, "ui")]


async def test_bootstrap_readonly_preview_file_change_and_preserved_ui(client, inbox_pool, tmp_path, monkeypatch):
    config = tmp_path / "config"
    shutil.copytree(files.CONFIG_DIR, config)
    monkeypatch.setattr(files, "CONFIG_DIR", config)
    path = config / "rules.yaml"
    configured = rule(id="managed-rule", owner="team:payments")
    path.write_text(yaml.safe_dump({"rules": [configured]}), encoding="utf-8")

    async with inbox_pool.connection() as conn, conn.transaction():
        await conn.execute("set transaction read only")
        preview = await loader.read_snapshot(conn)
        assert not preview.imported and preview.rules[0]["id"] == configured["id"]
        rows = await conn.execute("select count(*) from config_objects")
        assert await rows.fetchone() == (0,)

    async with loader.watch_config(inbox_pool):
        existing = await loader.snapshot(inbox_pool)
        assert {"origin": "file", "version": 1}.items() <= existing.rules[0].items()
        assert existing.directory["fallback_team"] and existing.sources and existing.schemas and existing.runbooks
        assert (await client.post("/v1/rules", json=rule(), headers={"X-User": "anna"})).status_code == 201

        async def edit_rules(entries):
            updated = path.with_suffix(".next")
            updated.write_text(yaml.safe_dump({"rules": entries}), encoding="utf-8")
            updated.replace(path)

        # Allow the async watcher to start watching before the atomic file replacement.
        await asyncio.sleep(0.25)
        configured["match"]["value"] = 25
        await edit_rules([configured])
        async with asyncio.timeout(6):
            while True:
                async with inbox_pool.connection() as conn:
                    current = await loader.read_snapshot(conn)
                if current.rules[0]["version"] == 2:
                    break
                await asyncio.sleep(0.05)
        assert {row["id"] for row in current.rules} == {configured["id"], rule()["id"]}
        await edit_rules([])
        await loader.sync_files(inbox_pool, force=True)
        remaining = await loader.snapshot(inbox_pool)
        assert [row["id"] for row in remaining.rules] == [rule()["id"]]
        async with inbox_pool.connection() as conn:
            rows = await conn.execute("""
                select version,active from config_objects where kind='rule' and id=%s order by version
            """, (configured["id"],))
            assert await rows.fetchall() == [(1, False), (2, False)]


async def test_preferences_runbook_and_team_edits_follow_directory(client, inbox_pool):
    anna = {"X-User": "anna"}
    lee = {"X-User": "lee"}
    ops = {"X-User": "ops"}
    directory = (await client.get("/v1/teams")).json()
    sources = (await client.get("/v1/sources")).json()["items"]
    assert sources and all(source["origin"] == "file" and "api_key" not in source for source in sources)
    user = next(user for user in directory["users"] if user["id"] == "anna")
    assert user["origin"] == "file" and "preferences" not in user
    assert (await client.get("/v1/users/anna/preferences", headers=lee)).status_code == 403
    assert (await client.put("/v1/users/anna/preferences", json=[], headers=lee)).status_code == 403
    rows = [{"min_severity": "info", "channels": ["ui"]}]
    assert (await client.put("/v1/users/anna/preferences", json=rows, headers=anna)).json() == rows
    assert (await client.get("/v1/users/anna/preferences", headers=anna)).json() == rows
    assert next(u for u in (await get_settings(inbox_pool)).directory["users"] if u["id"] == "anna") == {
        **user, "preferences": rows,
    }

    team = {"id": "new-team", "members": ["anna"], "oncall": {"primary": "anna"}, "approvers": ["anna"]}
    assert (await client.put("/v1/teams", json=team, headers=anna)).status_code == 403
    assert (await client.put("/v1/teams", json=team, headers=ops)).json()["version"] == 1
    assert (await client.put("/v1/teams", json={**team, "name": "New name"}, headers=anna)).json()["version"] == 2
    assert (await client.put("/v1/teams", json=team, headers=lee)).status_code == 403
    file_team = directory["teams"][0]
    assert (await client.put("/v1/teams", json=file_team, headers=ops)).status_code == 409

    content = "# First check\r\n1. Read the audit entry.\r\n"
    book = {"id": "new-book", "content": content}
    saved = await client.post("/v1/runbooks", json=book, headers=anna)
    assert saved.status_code == 201 and saved.json()["content"] == content and saved.json()["format"] == "markdown"
    assert (await client.post("/v1/runbooks", json=book, headers=lee)).status_code == 403
    book["id"] = (await client.get("/v1/runbooks")).json()["items"][0]["id"]
    assert (await client.post("/v1/runbooks", json=book, headers=ops)).status_code == 409


async def test_proposal_approval_is_atomic_and_team_scoped(client, inbox_pool):
    anna = {"X-User": "anna"}
    lee = {"X-User": "lee"}
    current = (await client.post("/v1/rules", json=rule(threshold=20), headers=anna)).json()
    await send_event(inbox_pool, "case")
    assert await process_batch(inbox_pool) == 1
    async with inbox_pool.connection() as conn:
        row = await conn.execute("select id from alerts where rule_id=%s", (current["id"],))
        alert_id = (await row.fetchone())[0]
    body = {"alert_id": alert_id, "text": "Only alert above 75", "draft": rule(threshold=75)}
    assert (await client.post("/v1/proposals", json=body, headers=lee)).status_code == 403
    response = await client.post("/v1/proposals", json=body, headers=anna)
    assert response.status_code == 201, response.text
    proposal = response.json()
    assert (proposal["base_version"], proposal["proposed_by"], proposal["source"]) == (1, "anna", "human")
    assert (await client.get("/v1/proposals", headers=lee)).json()["items"] == []
    assert len((await client.get("/v1/proposals?status=open", headers=anna)).json()["items"]) == 1
    endpoint = f"/v1/proposals/{proposal['id']}/approve"
    assert (await client.post(endpoint, headers=lee)).status_code == 403
    accepted = await client.post(endpoint, headers=anna)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["saved_rule"]["version"] == accepted.json()["approved_version"] == 2
    assert (await client.post(endpoint, headers=anna)).json()["approved_version"] == 2
    assert (await client.get("/v1/proposals?status=open", headers=anna)).json()["items"] == []
