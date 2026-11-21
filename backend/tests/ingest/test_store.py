import asyncio
from collections import Counter
from datetime import UTC, datetime

from alerting.contracts import Outcome
from alerting.ingest.normalize import normalize
from alerting.ingest.store import RejectedInput, save_batch


def event(message_id, value=1, source="test"):
    return normalize({"value": value}, {"id": source, "transport": "http"}, [],
                     received_at=datetime(2026, 10, 23, tzinfo=UTC), message_id=message_id)


async def test_one_thousand_concurrent_retransmissions(inbox_pool):
    entry = event("repeated")
    ready = asyncio.Barrier(10)

    async def send():
        async with inbox_pool.connection() as conn:
            await asyncio.wait_for(ready.wait(), timeout=15)
            return await save_batch(conn, [entry] * 100)

    batches = await asyncio.gather(*(send() for _ in range(10)))
    results = [r for batch in batches for r in batch]
    assert Counter(r.outcome for r in results) == {Outcome.NEEDS_REVIEW: 1, Outcome.DUPLICATE: 999}
    assert len({r.event_id for r in results}) == 1
    async with inbox_pool.connection() as conn:
        assert await (await conn.execute("SELECT count(*), min(status) FROM events")).fetchone() == (1, "needs_review")
        assert await (await conn.execute("SELECT count(*) FROM ingest_rejections WHERE kind='DUPLICATE'")).fetchone() == (999,)


async def test_conflict_is_recorded_and_distinct_occurrences_survive(inbox_pool):
    entries = [event("a"), event("a", 2), event("b"), event("a", source="another"), event(None), event(None),
               RejectedInput("test", None, "invalid JSON")]
    async with inbox_pool.connection() as conn:
        first, conflict, another_id, another_source, unkeyed1, unkeyed2, rejected = await save_batch(conn, entries)
    assert conflict.outcome == Outcome.CONFLICT and conflict.event_id == first.event_id
    assert rejected.outcome == Outcome.REJECTED and rejected.event_id is None
    assert len({r.event_id for r in (first, another_id, another_source, unkeyed1, unkeyed2)}) == 5
    async with inbox_pool.connection() as conn:
        original = await (await conn.execute("SELECT raw, fields FROM events WHERE id=%s", (first.event_id,))).fetchone()
        assert original[0] == {"value": 1} and original[1]["value"] == 1
        audit = await (await conn.execute("SELECT kind, existing_event_id FROM ingest_rejections ORDER BY id")).fetchall()
        assert audit == [("CONFLICT", first.event_id), ("REJECTED", None)]
