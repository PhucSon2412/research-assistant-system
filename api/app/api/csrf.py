"""CSRF protection for every state-changing request (POST/PUT/PATCH/DELETE).

1. An explicit valid ``Origin`` header must be listed in
   ``RAS_HTTP__ALLOWED_ORIGINS``.
2. The ``X-CSRF-Token`` header must equal the ``ras_csrf`` cookie (double
   submit). A cross-site page can neither read the cookie nor set the header
   without a CORS preflight, which this API never grants.
3. For authenticated requests the token must also match the hash stored on
   the session (``app/api/deps.py``), so a cookie planted by another site or
   subdomain is not enough.

The same middleware marks every response ``Cache-Control: no-store``.
"""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.cookies import CSRF_COOKIE, CSRF_HEADER
from app.config import get_settings, origin_allowed

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def install_csrf_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _csrf_and_headers(request: Request, call_next):
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if len(request.headers.getlist("origin")) != 1 or not origin_allowed(origin, get_settings().http.allowed_origins):
                return _no_store(JSONResponse({"error": "csrf_failed"}, status_code=403))
            header = request.headers.get(CSRF_HEADER)
            cookie = request.cookies.get(CSRF_COOKIE)
            if not header or not cookie or not hmac.compare_digest(header.encode(), cookie.encode()):
                return _no_store(JSONResponse({"error": "csrf_failed"}, status_code=403))
        return _no_store(await call_next(request))


def _no_store(response):
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response
