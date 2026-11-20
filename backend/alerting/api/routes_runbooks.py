"""Owner: DELIVERY. Runbooks, rendered steps, /runbooks/structure, actions, approvals, POST /v1/inbound/{channel} (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["runbooks"])
