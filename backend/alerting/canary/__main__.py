import asyncio
import os
from contextlib import asynccontextmanager, suppress

import httpx
import uvicorn
from fastapi import FastAPI, Request

from .monitor import Monitor, Settings


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        monitor = Monitor(client, Settings.from_env())
        app.state.monitor = monitor
        task = asyncio.create_task(monitor.run())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Alert Funnel canary", lifespan=lifespan)


@app.get("/status")
def status(request: Request):
    return request.app.state.monitor.status()


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("CANARY_HOST", "127.0.0.1"), port=int(os.getenv("CANARY_PORT", "8300")))
