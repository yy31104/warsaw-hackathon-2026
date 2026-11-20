"""Owner: DATA. Rules CRUD, /rules/test, /rules/{id}/preview, /rules/draft, proposals (calls BACKEND's pure engine) (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["rules"])
