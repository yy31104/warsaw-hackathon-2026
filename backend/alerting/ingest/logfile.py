"""Import a finite log file: python -m alerting.ingest.logfile --source pricing-logs [path]."""
import argparse
import asyncio
import json
import sys
from collections import Counter
from itertools import batched
from pathlib import Path

from alerting import db
from alerting.config import files
from alerting.ingest.inputs import prepare
from alerting.ingest.normalize import MAX_BYTES
from alerting.ingest.store import save_batch


def _lines(file):
    while line := file.readline(MAX_BYTES + 1):
        yield line
        # The first chunk of an overlong line reports one rejection. Resume at the
        # next real line instead of treating the remaining chunks as new events.
        while len(line) > MAX_BYTES and not line.endswith(b"\n"):
            line = file.readline(MAX_BYTES + 1)


async def import_file(path: Path, source: dict, schemas: list[dict], db_pool) -> dict:
    totals = Counter()
    with path.open("rb") as stream:
        for lines in batched(_lines(stream), 200):
            events = [prepare(line, source, schemas) for line in lines]
            async with db_pool.connection() as conn:
                results = await save_batch(conn, events)
            totals.update(result.outcome.value for result in results)
    return dict(totals)


async def run():
    sources = {s["id"]: s for s in files.sources() if s["transport"] == "logfile"}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=sources)
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    source = sources[args.source]
    await db.open_pool()
    try:
        counts = await import_file(args.path or Path(source["path"]), source, files.schemas(), db.pool())
        print(json.dumps(counts))
    finally:
        await db.close_pool()


if __name__ == "__main__":
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(), loop_factory=factory)
