"""Owner: DEV2. Rules CRUD, /rules/test, /rules/{id}/preview, /rules/draft, proposals (calls DEV3's pure engine) (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["rules"])
