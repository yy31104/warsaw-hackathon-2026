"""Demo actor checks against the same directory revision used for config writes."""

from contextlib import asynccontextmanager

from fastapi import Header, HTTPException

from alerting.config import loader
from alerting.config.store import ConfigError, lock_writes
from alerting.routing.directory import by_id


def actor_header(x_user: str | None = Header(None)):
    if not x_user:
        raise HTTPException(401, "Set X-User to the acting user")
    return x_user


def actor_teams(actor, directory):
    return [team["id"] for team in directory["teams"] if actor in team.get("members", [])
            or actor in team.get("approvers", [])]


def require_actor(actor, directory):
    if actor not in by_id(directory["users"]):
        raise ConfigError(403, "Unknown directory user")


def can_manage(actor, owner, directory):
    if owner == f"user:{actor}":
        return actor in by_id(directory["users"])
    if owner and owner.startswith("team:"):
        team = by_id(directory["teams"]).get(owner.removeprefix("team:"), {})
        return actor in team.get("approvers", [])
    return False


def require_owner(actor, owner, directory):
    if not can_manage(actor, owner, directory):
        raise ConfigError(403, "Rule or configuration owner approval is required")


@asynccontextmanager
async def config_write(db):
    try:
        await loader.sync_files(db)
        async with db.connection() as conn, conn.transaction():
            await lock_writes(conn)
            yield conn, await loader.read_snapshot(conn)
    except ConfigError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
