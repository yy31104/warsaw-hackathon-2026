"""Owner: DEV2. GET/PUT /v1/teams, GET/PUT /v1/users/{id}/preferences (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["teams"])
