"""Human-reviewed runbook drafting endpoints, mounted under the config router."""

from fastapi import APIRouter

from alerting.assist.runbook_structure import structure
from alerting.assist.text_input import TextInput


router = APIRouter(tags=["runbooks"])


@router.post("/runbooks/structure")
async def structure_runbook(body: TextInput):
    return await structure(body.text)
