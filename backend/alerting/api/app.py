"""FastAPI app. SHARED FILE — every router is already wired; owners edit only their routes_*.py."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from alerting.api import (
    routes_alerts,
    routes_ingest,
    routes_notifications,
    routes_ops,
    routes_rules,
    routes_runbooks,
    routes_schemas,
    routes_stream,
    routes_teams,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("DB_ON_STARTUP", "1") == "1":
        from alerting.db import close_pool, open_pool

        await open_pool()
        try:
            yield
        finally:
            await close_pool()
    else:
        yield


app = FastAPI(title="Alert Funnel", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (
    routes_ops,            # DATA
    routes_ingest,         # DATA
    routes_schemas,        # DATA
    routes_stream,         # DATA
    routes_rules,          # RULES
    routes_alerts,         # RULES
    routes_notifications,  # DELIVERY
    routes_runbooks,       # DELIVERY
    routes_teams,          # DELIVERY
):
    app.include_router(module.router)
