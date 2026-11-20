"""Owner: DEV4. GET /v1/stream (SSE via Postgres LISTEN/NOTIFY) (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["stream"])
