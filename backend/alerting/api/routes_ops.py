"""Owner: DATA. /health, /metrics, /v1/stats/funnel, /v1/debug/reset (CONTRACT §9)."""
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    # TODO(DATA): Prometheus text — events_ingested_total, events_processed_total, backlog, oldest_pending_age_s,
    # notifications_by_state, delivery_attempts_total, e2e_latency histogram.
    return "# alert-funnel metrics: not implemented yet\n"
