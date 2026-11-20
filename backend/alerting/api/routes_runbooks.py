from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from alerting.api.routes_alerts import get_alert, require_member
from alerting.rules.storage import Settings, get_db
from alerting.runbooks.approvals import decide, execute, request_run, runbook_for

router=APIRouter(prefix="/v1",tags=["runbooks"])


class StepRequest(BaseModel):
    step_index: int = Field(ge=0)


@router.get("/alerts/{id}/runbook")
async def runbook(id:int,db=Depends(get_db)):
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return await runbook_for(cur,await get_alert(cur,id),Settings.load())


@router.post("/alerts/{id}/actions")
async def start(id:int,body:StepRequest,x_user:str=Header(...),db=Depends(get_db)):
    settings=Settings.load()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        alert=await get_alert(cur,id,lock=True)
        require_member(alert,x_user,settings)
        steps=(await runbook_for(cur,alert,settings))["steps"]
        if body.step_index>=len(steps):
            raise HTTPException(422,"Unknown step")
        row=await request_run(cur,alert,steps[body.step_index],x_user,settings)
    if row["state"]=="requested":
        return await execute(db,row["id"],x_user,settings)
    return row


async def decision(db,id,actor,approve):
    settings=Settings.load()
    async with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row=await decide(cur,id,actor,approve,settings)
    return await execute(db,id,actor,settings) if approve else row


@router.post("/action-runs/{id}/approve")
async def approve(id:int,x_user:str=Header(...),db=Depends(get_db)):
    return await decision(db,id,x_user,True)


@router.post("/action-runs/{id}/reject")
async def reject(id:int,x_user:str=Header(...),db=Depends(get_db)):
    return await decision(db,id,x_user,False)
