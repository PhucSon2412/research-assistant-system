"""Route side of the M8a auth rate limits.

``enforce`` runs first in each protected handler, before the handler touches
the database Session (which connects lazily), so no connection, row or
journal lock is held while Redis answers. The recovery gate and the Origin/
CSRF middleware run earlier and keep their own answers.
"""

from __future__ import annotations

import logging

from fastapi import Request

from app.api.errors import ApiError
from app.config import Settings
from app.rate_limit import CLIENT_IP_HEADER, AuthAction, RateLimited, RateLimitUnavailable, client_address

log = logging.getLogger(__name__)

# Seconds a client should wait after the limiter itself was unavailable.
UNAVAILABLE_RETRY_AFTER = 30


def request_source(request: Request, settings: Settings) -> str:
    peer = request.client.host if request.client else None
    return client_address(peer, request.headers.getlist(CLIENT_IP_HEADER), settings.http.trusted_proxy_cidrs)


def enforce(request: Request, settings: Settings, limiter, action: AuthAction, *, account: str | None = None) -> None:
    try:
        limiter.check(action, source=request_source(request, settings), account=account)
    except RateLimited as exc:
        raise ApiError(429, "rate_limited", headers={"Retry-After": str(exc.retry_after)},
                       extra={"retry_after": exc.retry_after}) from None
    except RateLimitUnavailable as exc:
        log.error("auth rate limit unavailable (%s)", exc)
        raise ApiError(503, "rate_limit_unavailable",
                       headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER)}) from None
