"""Owner: DEV2 daizhongtian. POST /v1/events/{source_id}, GET /v1/events (query), GET /v1/events/{id} (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["ingest"])
