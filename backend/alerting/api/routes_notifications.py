"""Owner: DEV1 Yi. GET /v1/alerts/{id}/notifications, GET /v1/notifications, POST /v1/inbound/{channel} (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["notifications"])
