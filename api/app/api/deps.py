"""Request dependencies: DB session, clock, authenticated principal.

``get_session_context`` runs on every protected request: it resolves the
session cookie against the database (validity + idle/absolute expiry + user
active) and, for state-changing methods, checks the CSRF token is bound to
that session. Authorisation is then decided per resource (``app/authz.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.api.cookies import CSRF_HEADER, session_cookie_name
from app.api.csrf import SAFE_METHODS
from app.api.errors import ApiError
from app.authz import Principal
from app.clock import utcnow
from app.config import Settings, get_settings
from app.db import get_engine
from app.identity.sessions import SessionContext, resolve_session
from app.identity.tokens import digest_matches


def get_db() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def get_now() -> datetime:
    return utcnow()


def get_session_context(
    request: Request,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> SessionContext:
    raw = request.cookies.get(session_cookie_name(settings))
    if not raw:
        raise ApiError(401, "not_authenticated")
    ctx = resolve_session(db, raw, now=now, idle=settings.auth.session_idle)
    if ctx is None:
        db.rollback()
        raise ApiError(401, "session_expired", clear_session=True)
    db.commit()
    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER) or ""
        if not digest_matches(header, ctx.csrf_token_hash):
            raise ApiError(403, "csrf_failed")
    return ctx


def get_principal(ctx: SessionContext = Depends(get_session_context)) -> Principal:
    return Principal(
        user_id=ctx.user_id,
        role=ctx.role,
        session_id=ctx.session_id,
        email=ctx.email,
        display_name=ctx.display_name,
    )


def require_trusted_origin(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Sensitive writes also require an allowed ``Origin`` header; the CSRF
    middleware only rejects a disallowed one when the header is present."""
    if request.headers.get("origin") not in settings.http.allowed_origins:
        raise ApiError(403, "csrf_failed")


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "forbidden")
    return principal
