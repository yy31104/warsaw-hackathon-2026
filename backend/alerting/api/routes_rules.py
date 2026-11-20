"""Owner: DEV2 daizhongtian. Rules CRUD, /rules/test, /rules/{id}/preview, /rules/draft, proposals (calls Yi's pure engine) (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["rules"])
