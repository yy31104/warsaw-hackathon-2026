"""Directory and own-user notification preferences (CONTRACT §9)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from alerting.config import loader
from alerting.config.access import actor_header, config_write, require_actor, require_owner
from alerting.config.store import ConfigError, get_object, require_current_version, write_object
from alerting.config.validation import Preference, TeamDraft
from alerting.db import pool
from alerting.routing.directory import by_id, team_for

router = APIRouter(prefix="/v1", tags=["teams"])


@router.get("/teams")
async def list_teams(db=Depends(pool)):
    directory = await loader.directory(db)
    return {**directory, "users": [
        {key: value for key, value in user.items() if key != "preferences"} for user in directory["users"]
    ]}


class TeamUpdates(BaseModel):
    teams: list[TeamDraft] = Field(min_length=1)


@router.put("/teams")
async def update_teams(body: TeamDraft | TeamUpdates, actor=Depends(actor_header), db=Depends(pool)):
    drafts = body.teams if isinstance(body, TeamUpdates) else [body]
    if len({team.id for team in drafts}) != len(drafts):
        raise HTTPException(422, "Each team ID must appear once")
    async with config_write(db) as (conn, snapshot):
        directory = snapshot.directory
        require_actor(actor, directory)
        members = by_id(directory["users"])
        result = []
        for draft in drafts:
            current = await get_object(conn, "team", draft.id)
            if current and current.origin == "file":
                raise ConfigError(409, "This team is managed in a configuration file")
            team_owner = draft.id if current else directory["fallback_team"]
            require_owner(actor, f"team:{team_owner}", directory)
            require_current_version(current, draft.version)
            references = {*draft.members, *draft.approvers}
            if draft.oncall:
                references.add(draft.oncall.primary)
                if draft.oncall.secondary:
                    references.add(draft.oncall.secondary)
            if references.difference(members):
                raise ConfigError(422, "Team members, approvers and on-call recipients must exist in the directory")
            result.append(await write_object(conn, "team", draft.id, draft.spec(), current=current, origin="ui",
                                             actor=actor, team_id=draft.id))
        return {"items": result} if isinstance(body, TeamUpdates) else result[0]


def own_user(id, actor, directory):
    if actor != id:
        raise HTTPException(403, "Users can only access their own preferences")
    user = by_id(directory["users"]).get(id)
    if user is None:
        raise HTTPException(404, "Directory user not found")
    return user


@router.get("/users/{id}/preferences")
async def get_preferences(id: str, actor=Depends(actor_header), db=Depends(pool)):
    async with db.connection() as conn:
        directory = (await loader.read_snapshot(conn)).directory
        user = own_user(id, actor, directory)
        return user.get("preferences", directory["default_preferences"])


@router.put("/users/{id}/preferences")
async def put_preferences(id: str, preferences: list[Preference], actor=Depends(actor_header), db=Depends(pool)):
    rows = [pref.model_dump(mode="json", exclude_none=True) for pref in preferences]
    async with config_write(db) as (conn, snapshot):
        own_user(id, actor, snapshot.directory)
        current = await get_object(conn, "user_preferences", id)
        # A person's preferences are a separate UI object, leaving file-owned identity and contacts intact.
        await write_object(conn, "user_preferences", id, {"preferences": rows}, current=current, origin="ui",
                           actor=actor, team_id=team_for(f"user:{id}", snapshot.directory))
        return rows
