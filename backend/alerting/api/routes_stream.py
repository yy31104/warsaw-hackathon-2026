"""Owner: DATA. GET /v1/stream (SSE via Postgres LISTEN/NOTIFY), GET /v1/stats/funnel, POST /v1/debug/reset (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["stream"])
