"""Follow a configured log: python -m alerting.ingest.logtail --source ID [path].

The first run starts at byte zero. After a rename the tailer follows the
previous file to EOF. Keep renamed logs beside the active log until drained
so a restarted reader can recover the previous file.
"""
import argparse
import asyncio
import hashlib
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import psycopg
from psycopg_pool import PoolTimeout

from alerting import db
from alerting.config import files, loader
from alerting.config.store import get_object
from alerting.ingest.inputs import prepare
from alerting.ingest.normalize import MAX_BYTES
from alerting.ingest.store import RejectedInput, save_batch
from alerting.schemas.registry import ingest_registry

MAX_BATCH = 200
POLL_INTERVAL = 0.25
ANCHOR_BYTES = 128
log = logging.getLogger(__name__)


def frame_lines(data: bytes, discarding: bool = False) -> tuple[list[bytes | str], int, bool]:
    """Return records or rejection reasons, consumed bytes, and whether to skip to the next LF."""
    records: list[bytes | str] = []
    offset = 0
    while offset < len(data) and len(records) < MAX_BATCH:
        newline = data.find(b"\n", offset)
        end = len(data) if newline == -1 else newline + 1
        if not discarding:
            if end - offset > MAX_BYTES:
                records.append("log line exceeds 256 KiB")
            elif newline == -1:
                break  # Wait for the remainder of an ordinary partial line.
            else:
                records.append(data[offset:end])
        offset = end
        discarding = newline == -1
    return records, offset, discarding


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel.CreateFileW
    _create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _create_file.restype = wintypes.HANDLE
    _invalid_handle = wintypes.HANDLE(-1).value

    def _open_shared(path, flags):
        # The default Windows CRT opener prevents the rotator from renaming the file.
        generic_read, share_read_write_delete, open_existing = 0x80000000, 0x7, 3
        handle = _create_file(path, generic_read, share_read_write_delete, None, open_existing, 0, None)
        if handle == _invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except OSError:
            _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            _kernel.CloseHandle(handle)
            raise
else:
    def _open_shared(path, flags):
        return os.open(path, flags | os.O_NONBLOCK)


def _file_id(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{getattr(info, 'st_birthtime_ns', 0)}"


def _file_id_at(path: Path, *, follow_symlinks: bool = True) -> str | None:
    try:
        info = path.stat(follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        return None
    return _file_id(info) if stat.S_ISREG(info.st_mode) else None


def _anchor(stream: BinaryIO, offset: int) -> bytes | None:
    # Check the beginning and the committed boundary to detect copy-truncate
    # even if the new content grows beyond the previous offset between polls.
    if offset == 0:
        return b""
    size = min(offset, ANCHOR_BYTES)
    stream.seek(0)
    head = stream.read(size)
    stream.seek(offset - size)
    tail = stream.read(size)
    if len(head) != size or len(tail) != size:
        return None
    return hashlib.sha256(head + tail).digest()


@dataclass(frozen=True)
class Checkpoint:
    file_id: str | None
    offset: int = 0
    anchor: bytes = b""
    discarding: bool = False


class LogTail:
    def __init__(self, path: Path, source: dict):
        self.path = Path(os.path.abspath(path))
        self.source = source
        self._key = (source["id"], os.path.normcase(os.fspath(self.path)))
        self._stream: BinaryIO | None = None
        self._file_id: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
            self._file_id = None

    def _open(self, path: Path, expected: str | None = None):
        try:
            stream = open(path, "rb", buffering=0, opener=_open_shared)
        except FileNotFoundError:
            return
        info = os.fstat(stream.fileno())
        identity = _file_id(info)
        if stat.S_ISREG(info.st_mode) and expected in (None, identity):
            self._stream = stream
            self._file_id = identity
        else:
            stream.close()

    def _select_file(self, previous: str | None, current: str | None):
        if self._stream is not None and previous in (None, self._file_id):
            return
        self.close()
        if previous is not None and previous != current:
            # On restart, recover a renamed file that is still in the log directory.
            if self.path.parent.is_dir():
                for candidate in self.path.parent.iterdir():
                    if _file_id_at(candidate, follow_symlinks=False) == previous:
                        self._open(candidate, expected=previous)
                        if self._stream is not None:
                            return
            if current is not None:
                log.warning("Previous log file unavailable for %s; opening the current file", self.source["id"])
        if current is not None:
            self._open(self.path, expected=current)

    def _next_batch(self, checkpoint: Checkpoint) -> tuple[Checkpoint, list[bytes | str]] | None:
        current = _file_id_at(self.path)
        self._select_file(checkpoint.file_id, current)
        stream = self._stream
        if stream is None:
            return None

        start = checkpoint
        if self._file_id != start.file_id or _anchor(stream, start.offset) != start.anchor:
            start = Checkpoint(self._file_id)
        stream.seek(start.offset)
        data = stream.read(MAX_BYTES + 1)
        records, used, discarding = frame_lines(data, start.discarding)
        if used:
            end = start.offset + used
            anchor = _anchor(stream, end)
            stream.seek(start.offset)
            if anchor is None or stream.read(used) != data[:used] or _anchor(stream, start.offset) != start.anchor:
                return None  # The writer changed the bytes during this read. Recheck from the committed offset.
            return Checkpoint(start.file_id, end, anchor, discarding), records

        if current is not None and current != self._file_id and _file_id_at(self.path) == current:
            # Complete records in the previous generation have drained. A leftover
            # partial record must never join with the new file's first record.
            return Checkpoint(current), ["incomplete log line after rotation"] if data else []
        if start != checkpoint:
            return start, []
        return None

    async def poll(self, schemas, db_pool) -> bool:
        """Commit one bounded batch and its checkpoint. Return whether the cursor advanced."""
        if schemas is None:
            await loader.sync_files(db_pool)
        async with ingest_registry(db_pool, schemas) as (conn, active):
            await conn.execute("""
                INSERT INTO log_tail_checkpoints (source_id, path)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, self._key)
            checkpoint_row = await (await conn.execute("""
                SELECT file_id, byte_offset, anchor, discarding FROM log_tail_checkpoints
                WHERE source_id = %s AND path = %s FOR UPDATE
            """, self._key)).fetchone()
            batch = self._next_batch(Checkpoint(*checkpoint_row))
            if batch is None:
                return False
            checkpoint, records = batch
            source = self.source
            if schemas is None and records:
                # The running follower uses the committed parser for this unread batch.
                # The source path and checkpoint stay with this file across parser edits.
                current = await get_object(conn, "source", source["id"])
                if not current or not current.active or current.spec["transport"] != "logfile":
                    return False
                source = current.spec
            inputs = [
                prepare(line, source, active) if isinstance(line, bytes)
                else RejectedInput(self.source["id"], None, line)
                for line in records
            ]
            if inputs:
                await save_batch(conn, inputs)
            # The same row lock serializes replicas. A rollback keeps both
            # inbox rows and checkpoint at the previous committed batch.
            await conn.execute("""
                UPDATE log_tail_checkpoints
                SET file_id = %s, byte_offset = %s, anchor = %s, discarding = %s
                WHERE source_id = %s AND path = %s
            """, (checkpoint.file_id, checkpoint.offset, checkpoint.anchor, checkpoint.discarding, *self._key))
        return True


async def run():
    sources = {source["id"]: source for source in files.sources() if source["transport"] == "logfile"}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=sources)
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    source = sources[args.source]

    await db.open_pool()
    try:
        with LogTail(args.path or Path(source["path"]), source) as tail:
            log.info("Following %s for %s", tail.path, source["id"])
            while True:
                try:
                    if await tail.poll(None, db.pool()):
                        continue
                except (OSError, PoolTimeout, psycopg.OperationalError,
                        psycopg.errors.IdleInTransactionSessionTimeout):
                    log.exception("Log read or database connection interrupted; retrying the committed checkpoint")
                    await asyncio.sleep(1)
                    continue
                await asyncio.sleep(POLL_INTERVAL)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(), loop_factory=factory)
