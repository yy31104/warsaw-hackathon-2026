"""Owner: DEV2 daizhongtian. GET/POST /v1/runbooks (store, any format), POST /v1/runbooks/structure (LLM) (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["config"])
