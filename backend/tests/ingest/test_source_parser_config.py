import asyncio
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from alerting import db
from alerting.config import files, loader
from alerting.ingest import logfile, logtail


SOURCE = "tray-sensor"
ORIGINAL = r"^station=(?P<station>[A-Z]+)$"
UPDATED = r"^reading=(?P<reading>[0-9]+)$"
# A one-character lookbehind exercises the static check without matching any input.
UNSUPPORTED = r"^(?P<tray>tray)(?<=y)$"


@pytest.fixture
def edit_parser(tmp_path, monkeypatch):
    config = tmp_path / "config"
    shutil.copytree(files.CONFIG_DIR, config)
    monkeypatch.setattr(files, "CONFIG_DIR", config)
    path = config / "sources.yaml"

    def edit(pattern):
        source = {"id": SOURCE, "transport": "logfile", "path": str(tmp_path / "tray.log"),
                  "parser": {"type": "regex", "pattern": pattern}}
        updated = path.with_suffix(".next")
        updated.write_text(yaml.safe_dump({"sources": [source]}), encoding="utf-8")
        updated.replace(path)

    return edit


@pytest.mark.parametrize(("pattern", "reason"), [(UNSUPPORTED, "lookbehind"), ("(", "syntax"), (12, "string")])
def test_file_reader_and_cold_snapshot_reject_invalid_source_pattern(edit_parser, pattern, reason):
    edit_parser(pattern)
    with pytest.raises(ValueError, match=rf"(?i){SOURCE}.*{reason}"):
        files.sources()
    with pytest.raises(ValueError, match=rf"(?i){SOURCE}.*{reason}"):
        loader.build_snapshot(0, False, [])


@pytest.mark.parametrize("run", [logfile.run, logtail.run], ids=["logfile", "logtail"])
async def test_log_cli_rejects_invalid_source_before_opening_db(edit_parser, monkeypatch, run):
    edit_parser(UNSUPPORTED)
    monkeypatch.setattr(sys, "argv", ["log-reader", "--source", SOURCE])
    open_pool = AsyncMock(side_effect=AssertionError("unexpected DB access"))
    monkeypatch.setattr(db, "open_pool", open_pool)
    with pytest.raises(ValueError, match=rf"(?i){SOURCE}.*lookbehind"):
        await run()
    open_pool.assert_not_awaited()


async def test_watcher_keeps_last_committed_parser_and_accepts_a_corrected_edit(edit_parser, inbox_pool, caplog):
    async def versions():
        async with inbox_pool.connection() as conn:
            rows = await conn.execute("""
                SELECT version, active, spec->'parser'->>'pattern' FROM config_objects
                WHERE kind='source' AND id=%s ORDER BY version
            """, (SOURCE,))
            return await rows.fetchall()

    async def ingested():
        async with inbox_pool.connection() as conn:
            events = await (await conn.execute(
                "SELECT raw FROM events WHERE source_id=%s ORDER BY id", (SOURCE,),
            )).fetchall()
            rejections = await (await conn.execute(
                "SELECT kind, reason FROM ingest_rejections WHERE source_id=%s ORDER BY id", (SOURCE,),
            )).fetchall()
        return events, rejections

    edit_parser(UNSUPPORTED)
    with pytest.raises(ValueError, match=rf"(?i){SOURCE}.*lookbehind"):
        await loader.sync_files(inbox_pool, force=True)
    assert await versions() == []

    edit_parser(ORIGINAL)
    (source,) = files.sources()
    path = Path(source["path"])
    path.write_bytes(b"station=NORTH\n")
    with logtail.LogTail(path, source) as tail:
        async with loader.watch_config(inbox_pool):
            assert await versions() == [(1, True, ORIGINAL)]
            assert await tail.poll(None, inbox_pool)
            # Let watchfiles subscribe before synchronizing on the attempted bad reload.
            await asyncio.sleep(0.25)
            edit_parser(UNSUPPORTED)
            async with asyncio.timeout(6):
                while not any(record.exc_info and SOURCE in str(record.exc_info[1]) for record in caplog.records):
                    await asyncio.sleep(0.05)
            assert "lookbehind" in caplog.text.lower()
            assert await versions() == [(1, True, ORIGINAL)]

            with path.open("ab") as stream:
                stream.write(b"station=SOUTH\n")
            assert await tail.poll(None, inbox_pool)
            previous_events = [({"station": "NORTH"},), ({"station": "SOUTH"},)]
            assert await ingested() == (previous_events, [])

            edit_parser(UPDATED)
            async with asyncio.timeout(6):
                while await versions() != [(1, False, ORIGINAL), (2, True, UPDATED)]:
                    await asyncio.sleep(0.05)

            with path.open("ab") as stream:
                stream.write(b"reading=17\n")
            assert await tail.poll(None, inbox_pool)
            assert not await tail.poll(None, inbox_pool)

    assert await ingested() == (previous_events + [({"reading": "17"},)], [])
