from pathlib import Path

from alerting.config import files
from alerting.ingest.logfile import import_file
from alerting.ingest.normalize import MAX_BYTES


async def test_file_import_retains_real_occurrences_and_reports_bad_lines(inbox_pool, tmp_path):
    source = next(s for s in files.sources() if s["transport"] == "logfile")
    sample = Path(__file__).resolve().parents[3] / "fixtures/events/pricing-engine.log"
    path = tmp_path / "sample.log"
    lines = sample.read_bytes().splitlines()
    path.write_bytes(b"\n".join([*lines, lines[1], b"x" * (2 * MAX_BYTES + 4), b"broken", lines[1]]))
    assert await import_file(path, source, files.schemas(), inbox_pool) == {"ACCEPTED": 9, "REJECTED": 2}
    async with inbox_pool.connection() as conn:
        counts = await (await conn.execute("""
            SELECT severity, count(*) FROM events WHERE source_event_id IS NULL GROUP BY severity
        """)).fetchall()
        assert dict(counts) == {"warning": 6, "error": 1, "info": 2}
