import shutil

import yaml

from alerting.config import files, loader
from alerting.ingest.logtail import LogTail


async def test_pasted_parser_applies_to_next_record_without_replaying_rejections(
    client, inbox_pool, monkeypatch, tmp_path,
):
    config = tmp_path / "config"
    shutil.copytree(files.CONFIG_DIR, config)
    monkeypatch.setattr(files, "CONFIG_DIR", config)
    monkeypatch.setenv("ASSIST_ENABLED", "0")
    source_file = config / "sources.yaml"
    sources = yaml.safe_load(source_file.read_text())
    path = tmp_path / "rotor.log"
    source = {"id": "rotor", "transport": "logfile", "path": str(path),
              "parser": {"type": "regex", "pattern": r"old (?P<message>.+)"}}
    sources["sources"].append(source)
    source_file.write_text(yaml.safe_dump(sources))
    await loader.sync_files(inbox_pool, force=True)

    path.write_text("a|b|c\n")
    with LogTail(path, source) as tail:
        assert await tail.poll(None, inbox_pool)
        suggestion = await client.post("/v1/sources/rotor/parser/suggest",
                                       json={"lines": ["a|b|c", "d|e|f", "g|h|i"]})
        assert suggestion.status_code == 200, suggestion.text
        pasted = yaml.safe_load(suggestion.json()["parser_yaml"])
        sources["sources"][-1] = {**source, **pasted}
        source_file.write_text(yaml.safe_dump(sources))
        await loader.sync_files(inbox_pool, force=True)  # Same import the file watcher commits.

        with path.open("a") as stream:
            stream.write("d|e|f\n")
        assert await tail.poll(None, inbox_pool)
        assert not await tail.poll(None, inbox_pool)

    async with inbox_pool.connection() as conn:
        rows = await (await conn.execute("SELECT raw FROM events WHERE source_id='rotor'")).fetchall()
        rejections = await (await conn.execute(
            "SELECT count(*) FROM ingest_rejections WHERE source_id='rotor'"
        )).fetchone()
    assert rows == [(suggestion.json()["preview"][1]["fields"],)]
    assert rejections == (1,)
