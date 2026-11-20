"""Owner: DELIVERY. GET /v1/alerts/{id}/notifications, GET /v1/notifications (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["notifications"])
