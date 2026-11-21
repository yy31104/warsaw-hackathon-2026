"""Local SMS/phone receiver and reply gateway. Run on port 8100."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from alerting.clock import SYSTEM
from .inbox import Inbox, ReceiptConflict


HERE = Path(__file__).resolve().parent
Mode = Literal["ok", "down", "503", "slow", "drop_response"]
NotificationKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=256, pattern=r"^[!-~][ -~]*$")
]
GeneratorTime = Annotated[int | None, Header(alias="X-Gen-Sent-At", ge=0, le=2**63 - 1)]


class Notice(BaseModel):
    to: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4096)


class ModeChange(BaseModel):
    mode: Mode


class Reply(BaseModel):
    sender: str = Field(alias="from", min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=64)


class LostAcknowledgement(Response):
    """The receipt is durable, but the sender receives no headers or body before its timeout."""

    async def __call__(self, scope, receive, send):
        while (await receive())["type"] != "http.disconnect":
            continue


def create_app(*, inbound_transport: httpx.AsyncBaseTransport | None = None):
    inbox = Inbox(Path(os.getenv("FAKE_TELECOM_DB", HERE / ".data" / "receipts.sqlite3")))
    api_url = os.getenv("ALERTING_API_URL", "http://127.0.0.1:8000").rstrip("/")
    secret = os.getenv("INBOUND_SECRET")

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(inbox.initialize)
        async with httpx.AsyncClient(
            base_url=api_url, transport=inbound_transport, timeout=5, trust_env=False
        ) as api:
            app.state.api = api
            yield

    app = FastAPI(title="Phone simulator", lifespan=lifespan)

    async def receive(channel, notice, key, gen_sent_at):
        received_at = int(SYSTEM.now().timestamp() * 1000)
        try:
            mode = await run_in_threadpool(
                inbox.receive, key, channel, notice.to, notice.text, gen_sent_at, received_at
            )
        except ReceiptConflict:
            raise HTTPException(409, "This notification key belongs to a different message") from None
        if mode in ("down", "503"):
            headers = {"Retry-After": "2"} if mode == "503" else None
            raise HTTPException(503, "Gateway unavailable", headers=headers)
        if mode == "drop_response":
            return LostAcknowledgement()
        if mode == "slow":
            await asyncio.sleep(4)  # Yi's delivery client times out after three seconds.
        return JSONResponse({"id": key}, headers={"X-Message-Id": key})

    @app.post("/sms")
    async def sms(notice: Notice, key: NotificationKey, gen_sent_at: GeneratorTime = None):
        return await receive("sms", notice, key, gen_sent_at)

    @app.post("/call")
    async def call(notice: Notice, key: NotificationKey, gen_sent_at: GeneratorTime = None):
        return await receive("phone", notice, key, gen_sent_at)

    @app.get("/messages")
    def messages(to: str | None = None, limit: Annotated[int | None, Query(ge=1)] = None):
        return {"items": inbox.messages(to, limit)}

    @app.get("/numbers")
    def numbers():
        return {"items": inbox.numbers()}

    @app.get("/mode")
    def current_mode():
        return {"mode": inbox.mode()}

    @app.post("/mode")
    def change_mode(change: ModeChange):
        inbox.set_mode(change.mode)
        return {"mode": change.mode}

    @app.post("/reply")
    async def reply(message: Reply):
        if not secret:
            raise HTTPException(503, "Set INBOUND_SECRET on the gateway and alert API to enable replies")
        if not await run_in_threadpool(inbox.messages, message.sender, 1):
            raise HTTPException(404, "This phone has no received notifications")
        try:
            result = await app.state.api.post(
                "/v1/inbound/sms", json=message.model_dump(by_alias=True), headers={"X-Provider-Secret": secret}
            )
        except httpx.TimeoutException:
            raise HTTPException(504, "The alert API did not confirm the reply in time") from None
        except httpx.RequestError:
            raise HTTPException(502, "Could not reach the alert API") from None
        if result.is_success:
            return {"status": "sent"}
        if result.is_error:
            try:
                detail = result.json()["detail"]
            except (ValueError, KeyError, TypeError):
                detail = "The alert API rejected the reply"
            headers = {"Retry-After": result.headers["Retry-After"]} if "Retry-After" in result.headers else None
            raise HTTPException(result.status_code, detail, headers=headers)
        raise HTTPException(502, "Unexpected response from the alert API")

    @app.get("/", include_in_schema=False)
    def phone():
        return FileResponse(HERE / "index.html", media_type="text/html")

    return app


app = create_app()
