"""Owner: RULES. Rules CRUD, /rules/test, /rules/{id}/preview, /rules/draft, proposals, GET /v1/events/{id}/explain (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["rules"])
