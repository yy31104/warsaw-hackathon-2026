"""Owner: BACKEND. GET /v1/alerts/{id}/runbook (rendered steps), actions, approvals, POST /v1/inbound/{channel} (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["runbooks"])
