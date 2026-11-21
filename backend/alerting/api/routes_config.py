"""Store and load original runbook documents (CONTRACT §9)."""

from contextlib import asynccontextmanager
import os

from fastapi import APIRouter, Depends

from alerting.api.routes_parser_assist import router as parser_assist_router
from alerting.api.routes_runbook_assist import router as assist_router
from alerting.config import loader
from alerting.config.access import actor_header, config_write, require_owner
from alerting.config.store import ConfigError, get_object, require_current_version, write_object
from alerting.config.validation import RunbookDraft
from alerting.db import pool
from alerting.routing.directory import team_for


@asynccontextmanager
async def lifespan(_app):
    if os.getenv("DB_ON_STARTUP", "1") == "1":
        async with loader.watch_config(pool()):
            yield
    else:
        yield


router = APIRouter(prefix="/v1", tags=["config"], lifespan=lifespan)
router.include_router(assist_router)
router.include_router(parser_assist_router)


@router.get("/sources")
async def list_sources(db=Depends(pool)):
    fields = {"id", "transport", "queue", "path", "parser", "split_path", "dedup", "version", "origin"}
    return {"items": [{key: value for key, value in source.items() if key in fields}
                      for source in await loader.sources(db)]}


@router.get("/runbooks")
async def list_runbooks(db=Depends(pool)):
    return {"items": await loader.runbooks(db)}


@router.post("/runbooks", status_code=201)
async def publish_runbook(draft: RunbookDraft, actor=Depends(actor_header), db=Depends(pool)):
    async with config_write(db) as (conn, snapshot):
        directory = snapshot.directory
        current = await get_object(conn, "runbook", draft.id)
        if current and current.origin == "file":
            raise ConfigError(409, "This runbook is managed in a configuration file")
        previous_owner = current.spec.get("owner") if current else None
        if current:
            require_owner(actor, previous_owner or f"team:{directory['fallback_team']}", directory)
            require_current_version(current, draft.version)
        owner = draft.owner or previous_owner or f"user:{actor}"
        require_owner(actor, owner, directory)
        # An unscoped match can choose a book for an unrelated rule.
        if draft.has_global_match():
            require_owner(actor, f"team:{directory['fallback_team']}", directory)
        rules = {rule["id"]: rule for rule in snapshot.rules}
        for id in draft.rules or []:
            if id not in rules:
                raise ConfigError(422, f"Unknown rule '{id}'")
            require_owner(actor, rules[id].get("owner"), directory)
        spec = {**draft.spec(), "owner": owner}
        return await write_object(conn, "runbook", draft.id, spec, current=current, origin="ui",
                                  actor=actor, team_id=team_for(owner, directory), audit_source=draft.source)
