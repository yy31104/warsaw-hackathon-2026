import asyncio
from pathlib import Path

import pytest

from alerting.config import files
from alerting.ingest import logtail
from alerting.ingest.logtail import LogTail
from alerting.ingest.normalize import MAX_BYTES


@pytest.fixture
def pricing():
    source = next(source for source in files.sources() if source["id"] == "pricing-logs")
    fixture = Path(__file__).resolve().parents[3] / "fixtures/events/pricing-engine.log"
    return source, files.schemas(), fixture.read_bytes().splitlines(keepends=True)


def append(path, body):
    with path.open("ab") as stream:
        stream.write(body)


async def drain(tail, schemas, pool):
    for _ in range(10):
        if not await tail.poll(schemas, pool):
            return
    pytest.fail("The log checkpoint kept advancing after the file drained")


async def stored(pool):
    async with pool.connection() as conn:
        events = await (await conn.execute("SELECT severity, source_event_id FROM events ORDER BY id")).fetchall()
        errors = await (await conn.execute("SELECT kind, reason FROM ingest_rejections ORDER BY id")).fetchall()
    return events, errors


async def test_tail_waits_for_a_partial_line_across_restart_and_keeps_real_repeats(inbox_pool, tmp_path, pricing):
    source, schemas, lines = pricing
    path = tmp_path / "current.log"
    second = lines[1].replace(b"price latency", "price \N{EURO SIGN} latency".encode())
    split = second.index(b"\xe2") + 1
    with LogTail(path, source) as tail:
        assert not await tail.poll(schemas, inbox_pool)
        path.write_bytes(lines[0] + second[:split])
        await drain(tail, schemas, inbox_pool)
        assert await stored(inbox_pool) == ([("info", None)], [])

    with LogTail(path, source) as restarted:
        await drain(restarted, schemas, inbox_pool)
        assert await stored(inbox_pool) == ([("info", None)], [])
        append(path, second[split:] + second)
        await drain(restarted, schemas, inbox_pool)
    assert await stored(inbox_pool) == ([("info", None), ("warning", None), ("warning", None)], [])


@pytest.mark.parametrize("restart", [False, True])
async def test_tail_drains_rotated_file_before_opening_replacement(inbox_pool, tmp_path, pricing, restart):
    source, schemas, lines = pricing
    path = tmp_path / "current.log"
    # A running tail can use the retained handle even after a move to another
    # directory. A restarted tail discovers an archive in the log directory.
    archive_path = tmp_path / "current.log.1" if restart else tmp_path / "archive" / "current.log.1"
    archive_path.parent.mkdir(exist_ok=True)
    path.write_bytes(lines[0])

    with LogTail(path, source) as tail, LogTail(path, source) as second_tail:
        await drain(tail, schemas, inbox_pool)
        reader = tail
        if restart:
            tail.close()
            reader = second_tail
        path.rename(archive_path)
        append(archive_path, lines[1] + b"unfinished")
        # A writer can rotate in two steps with a gap before creating the new file.
        await drain(reader, schemas, inbox_pool)
        assert await stored(inbox_pool) == ([("info", None), ("warning", None)], [])
        path.write_bytes(lines[3])
        await drain(reader, schemas, inbox_pool)
    assert await stored(inbox_pool) == (
        [("info", None), ("warning", None), ("error", None)],
        [("REJECTED", "incomplete log line after rotation")],
    )


@pytest.mark.parametrize("copies", [1, 5])
async def test_tail_recovers_from_copy_truncate_even_after_regrowth(inbox_pool, tmp_path, pricing, copies):
    source, schemas, lines = pricing
    path = tmp_path / "current.log"
    path.write_bytes(lines[0] + lines[1])
    before = path.stat()
    with LogTail(path, source) as tail:
        await drain(tail, schemas, inbox_pool)
        path.write_bytes(lines[3] * copies)
        after = path.stat()
        assert after.st_ino == before.st_ino
        assert (after.st_size > before.st_size) == (copies > 1)
        await drain(tail, schemas, inbox_pool)
    assert await stored(inbox_pool) == ([("info", None), ("warning", None)] + [("error", None)] * copies, [])


async def test_tail_records_an_oversized_line_once_across_restart_and_resumes(inbox_pool, tmp_path, pricing):
    source, schemas, lines = pricing
    path = tmp_path / "current.log"
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    with LogTail(path, source) as tail:
        await drain(tail, schemas, inbox_pool)
    assert await stored(inbox_pool) == ([], [("REJECTED", "log line exceeds 256 KiB")])
    append(path, b"x" * MAX_BYTES + b"\n" + lines[0])
    with LogTail(path, source) as restarted:
        await drain(restarted, schemas, inbox_pool)
    assert await stored(inbox_pool) == ([("info", None)], [("REJECTED", "log line exceeds 256 KiB")])


async def test_tail_serializes_concurrent_readers_of_the_same_checkpoint(inbox_pool, tmp_path, pricing):
    source, schemas, lines = pricing
    path = tmp_path / "current.log"
    count = logtail.MAX_BATCH + 5
    path.write_bytes(lines[0] * count + b"not a configured record\n")
    with LogTail(path, source) as first, LogTail(path, source) as second:
        await asyncio.gather(drain(first, schemas, inbox_pool), drain(second, schemas, inbox_pool))
    events, errors = await stored(inbox_pool)
    assert events == [("info", None)] * count
    assert errors == [("REJECTED", "log line does not match the configured parser")]


async def test_tail_retries_the_entire_batch_if_the_checkpoint_transaction_rolls_back(
    inbox_pool, tmp_path, pricing, monkeypatch,
):
    source, schemas, lines = pricing
    path = tmp_path / "current.log"
    path.write_bytes(lines[0] + b"bad record\n")
    save_batch = logtail.save_batch

    async def fail_after_writing(*args):
        await save_batch(*args)
        raise RuntimeError("fail before checkpoint commit")

    with LogTail(path, source) as tail:
        with monkeypatch.context() as patch:
            patch.setattr(logtail, "save_batch", fail_after_writing)
            with pytest.raises(RuntimeError, match="fail before checkpoint commit"):
                await tail.poll(schemas, inbox_pool)
        assert await stored(inbox_pool) == ([], [])
        await drain(tail, schemas, inbox_pool)
    assert await stored(inbox_pool) == (
        [("info", None)], [("REJECTED", "log line does not match the configured parser")],
    )
