"""Owner: DATA. GET /v1/sources, GET/POST /v1/schemas, GET /v1/schema-issues, POST /v1/schemas/suggest (docs/CONTRACT.md §9)."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["schemas"])
