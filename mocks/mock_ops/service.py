"""Simulated runbook targets and persistent execution receipts."""

import hashlib
import json
import os
import sqlite3
from asyncio import sleep
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException


FailureCount = Annotated[int, Field(ge=0, le=2**63 - 1)]
DemoAction = Literal["retry_workflow", "restart_service", "open_ticket"]
DEMO_DELAY_SECONDS = 4.0


class Mode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    healthy: bool | None = None
    fail_n: FailureCount | None = None


class RetryConfig(Mode):
    fail_n: FailureCount


class DelayNext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: DemoAction


class DelayedDemoResponse(JSONResponse):
    async def __call__(self, scope, receive, send):
        # ASGI sends this response after perform() has committed and closed SQLite.
        await sleep(DEMO_DELAY_SECONDS)
        await super().__call__(scope, receive, send)


def execution_receipt(status: int, output: dict) -> dict:
    ok = 200 <= status < 300 and output.get("ok", True) is not False
    return {
        "simulated": True,
        "state": "succeeded" if ok else "failed",
        "result": {
            "ok": ok,
            "output": output,
            "error": f"mock-ops HTTP {status}" if status >= 400 else None,
            "resolves_alert": ok and output.get("resolves_alert") is True,
        },
    }


def create_app(path: str | Path) -> FastAPI:
    demo_enabled = os.getenv("DEMO_MODE") == "1"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            create table if not exists settings (
                id integer primary key, healthy integer not null, fail_n integer not null
            );
            insert or ignore into settings values (1, 1, 0);
            create table if not exists workflows (id text primary key, attempts integer not null);
            create table if not exists receipts (
                key text primary key, fingerprint text not null, status integer not null,
                body text not null
            );
        """)
        if demo_enabled:
            db.execute("create table if not exists demo_delays (action text primary key)")
    app = FastAPI(title="Simulated mock-ops")

    if demo_enabled:
        @app.post("/demo/delay-next")
        def delay_next(body: DelayNext):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("insert or ignore into demo_delays(action) values (?)", (body.action,))
            return {"simulated": True, "action": body.action, "delay_seconds": DEMO_DELAY_SECONDS}

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            {"simulated": True, "detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(
            {"simulated": True, "detail": jsonable_encoder(exc.errors())}, status_code=422
        )

    def perform(
        key: str | None,
        method: str,
        route: str,
        params: dict,
        operation: Callable[[sqlite3.Connection], tuple[int, dict]],
        *,
        delay_action: DemoAction | None = None,
    ) -> JSONResponse:
        if key is not None and not key.strip():
            raise HTTPException(422, "Idempotency-Key must be nonempty")
        with closing(sqlite3.connect(path)) as db, db:
            # A concurrent replay must see the receipt before it can apply the same effect.
            db.execute("begin immediate" if key else "begin")
            if key:
                arguments = json.dumps([method, route, params], sort_keys=True, separators=(",", ":"))
                signature = hashlib.sha256(arguments.encode()).hexdigest()
                previous = db.execute(
                    "select fingerprint, status, body from receipts where key=?", (key,)
                ).fetchone()
                if previous:
                    if signature != previous[0]:
                        raise HTTPException(
                            409, "Idempotency-Key already used for a different operation"
                        )
                    return JSONResponse(json.loads(previous[2]), status_code=previous[1])

            status, fields = operation(db)
            output = {"simulated": True, **fields}
            if key:
                db.execute(
                    "insert into receipts values (?, ?, ?, ?)",
                    (key, signature, status, json.dumps(output)),
                )
                if demo_enabled and delay_action is not None:
                    armed = db.execute(
                        "delete from demo_delays where action=? returning action", (delay_action,)
                    ).fetchone()
                    if armed:
                        return DelayedDemoResponse(output, status_code=status)
            return JSONResponse(output, status_code=status)

    def update_mode(route: str, key: str, mode: Mode):
        def change(db):
            # Empty /toggle flips health. Setting only the retry threshold preserves health.
            flip = route == "/toggle" and mode.healthy is None and mode.fail_n is None
            healthy, fail_n = db.execute(
                """update settings set
                   healthy=case when ? then 1-healthy else coalesce(?, healthy) end,
                   fail_n=coalesce(?, fail_n) where id=1 returning healthy, fail_n""",
                (flip, mode.healthy, mode.fail_n),
            ).fetchone()
            return 200, {"ok": True, "healthy": bool(healthy), "fail_n": fail_n}

        return perform(key, "POST", route, mode.model_dump(), change)

    @app.post("/config")
    def configure(body: RetryConfig, idempotency_key: str = Header(...)):
        return update_mode("/config", idempotency_key, body)

    @app.post("/toggle")
    @app.post("/mode", include_in_schema=False)
    def toggle(request: Request, idempotency_key: str = Header(...), body: Mode | None = None):
        return update_mode(request.url.path, idempotency_key, body or Mode())

    @app.get("/health")
    def health(idempotency_key: str | None = Header(None)):
        def read(db):
            healthy = bool(db.execute("select healthy from settings where id=1").fetchone()[0])
            return 200, {"healthy": healthy, "ok": healthy, "resolves_alert": healthy}

        return perform(idempotency_key, "GET", "/health", {}, read)

    @app.post("/workflows/{workflow}/retry")
    def retry(workflow: str, idempotency_key: str = Header(...)):
        def attempt(db):
            count = db.execute(
                """insert into workflows values (?, 1) on conflict(id)
                   do update set attempts=attempts+1 returning attempts""",
                (workflow,),
            ).fetchone()[0]
            fail_n = db.execute("select fail_n from settings where id=1").fetchone()[0]
            ok = count > fail_n
            return (200 if ok else 503), {"workflow": workflow, "attempt": count, "ok": ok}

        return perform(idempotency_key, "POST", "/workflows/retry", {"workflow": workflow}, attempt,
                       delay_action="retry_workflow")

    @app.post("/services/{name}/restart")
    def restart(name: str, idempotency_key: str = Header(...)):
        def action(db):
            return 200, {"ok": True, "service": name, "restart_id": uuid4().hex}

        return perform(idempotency_key, "POST", "/services/restart", {"service": name}, action,
                       delay_action="restart_service")

    @app.post("/tickets")
    def ticket(body: dict, idempotency_key: str = Header(...)):
        def issue(db):
            return 201, {"ok": True, "ticket_id": uuid4().hex, "title": body.get("title")}

        return perform(idempotency_key, "POST", "/tickets", body, issue, delay_action="open_ticket")

    @app.get("/metrics/pool")
    def metrics(service: str = "counterparty-service", idempotency_key: str | None = Header(None)):
        def read(db):
            return 200, {"service": service, "active": 8, "capacity": 20}

        return perform(idempotency_key, "GET", "/metrics/pool", {"service": service}, read)

    @app.get("/deployments")
    def deployments(
        service: str = "counterparty-service", idempotency_key: str | None = Header(None)
    ):
        def read(db):
            return 200, {"items": [{"service": service, "revision": "demo-1", "simulated": True}]}

        return perform(idempotency_key, "GET", "/deployments", {"service": service}, read)

    @app.get("/executions/{key:path}")
    def execution(key: str):
        with closing(sqlite3.connect(path)) as db:
            row = db.execute("select status, body from receipts where key=?", (key,)).fetchone()
        if row is None:
            raise HTTPException(404, "No recorded execution")
        return execution_receipt(row[0], json.loads(row[1]))

    return app


def app_factory() -> FastAPI:
    return create_app(os.getenv("MOCK_OPS_DB", "/tmp/alert-funnel-mock-ops.sqlite3"))
