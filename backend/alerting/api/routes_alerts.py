"""Owner: RULES. GET /v1/alerts, GET /v1/alerts/{id}, POST ack/resolve (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["alerts"])
