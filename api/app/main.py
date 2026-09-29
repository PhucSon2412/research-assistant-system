"""FastAPI entrypoint.

M1b adds invitation/session/reset auth, Admin account management and the
minimal library metadata API. M1c adds private upload intent and originals.
M6a adds owner review requests for one exact private version/file.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.csrf import install_csrf_middleware
from app.api.errors import install_error_handlers
from app.api.routes import admin, auth, library, uploads, search, collections, rag, discovery, reviews
from app.db import get_engine

app = FastAPI(title="Research Assistant System API", version="0.2.0")
install_error_handlers(app)
install_csrf_middleware(app)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(library.router)
app.include_router(uploads.router)
app.include_router(search.router)
app.include_router(collections.router)
app.include_router(rag.router)
app.include_router(discovery.router)
app.include_router(reviews.router)
app.include_router(reviews.admin_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness: the database is reachable and migrated."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1 FROM alembic_version"))
    except Exception:
        # Do not echo driver errors: they may contain connection details.
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})
