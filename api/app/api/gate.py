"""Recovery gate middleware (M6c-1): no request is served while the gate is closed.

Runs before routing, so it covers every route (library, files, chunks,
search, RAG, collections, Admin, auth) without relying on the healthcheck or
Compose start order. Only ``/healthz`` (liveness) and ``/readyz`` (which
reports the gate itself) pass. The answer is ``503 service_recovering`` with
no detail: the reason code goes to the log only.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import recovery
from app.config import get_settings
from app.db import get_engine

EXEMPT = frozenset({"/healthz", "/readyz"})


def install_recovery_gate(app: FastAPI) -> None:
    @app.middleware("http")
    async def _recovery_gate(request: Request, call_next):
        if request.url.path in EXEMPT:
            return await call_next(request)
        status = await run_in_threadpool(recovery.evaluate, get_settings(), get_engine())
        recovery.log_if_changed(status, "api")
        if not status.open:
            return JSONResponse({"error": "service_recovering"}, status_code=503,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                         "Retry-After": "60"})
        return await call_next(request)
