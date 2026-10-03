"""Request dependencies: DB session, clock, authenticated principal.

``get_session_context`` runs on every protected request: it resolves the
session cookie against the database (validity + idle/absolute expiry + user
active) and, for state-changing methods, checks the CSRF token is bound to
that session. Authorisation is then decided per resource (``app/authz.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.cookies import CSRF_HEADER, session_cookie_name
from app.api.csrf import SAFE_METHODS
from app.api.errors import ApiError
from app.authz import Principal
from app.clock import utcnow
from app.config import Settings, get_settings, origin_allowed
from app.db import get_engine
from app.identity.sessions import SessionContext, resolve_session, session_is_live
from app.identity.predicates import lock_users, session_live
from app.auth_safety import require_writer
from app.models import UserSession
from app.identity.session_commit import commit_activity
from app.identity.tokens import digest_matches


def get_db() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def get_now() -> datetime:
    return utcnow()


def get_clock() -> Callable[[], datetime]:
    """The current time on demand, for a check made after slow work in the same
    request (``get_now`` is fixed when the request starts)."""
    return utcnow


def get_session_context(
    request: Request,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
    clock=Depends(get_clock),
) -> SessionContext:
    raw = request.cookies.get(session_cookie_name(settings))
    if not raw:
        raise ApiError(401, "not_authenticated")
    ctx = resolve_session(db, raw, now=now, idle=settings.auth.session_idle, clock=clock)
    if ctx is None:
        db.rollback()
        raise ApiError(401, "session_expired", clear_session=True)
    commit_activity(db)
    try:
        if not session_is_live(db, ctx.session_id, now=clock(), idle=settings.auth.session_idle, clock=clock):
            raise ApiError(401, "session_expired", clear_session=True)
        final_csrf = db.get(UserSession, ctx.session_id, populate_existing=True).csrf_token_hash
    finally:
        db.rollback()  # release final read journal lock before any downstream row locks
    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER) or ""
        if not digest_matches(header, final_csrf):
            raise ApiError(403, "csrf_failed")
    return ctx


def get_auth_write_context(request: Request, ctx: SessionContext = Depends(get_session_context),
                           db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                           clock=Depends(get_clock)) -> SessionContext:
    require_writer(db)
    lock_users(db, [ctx.user_id])
    if not session_live(db, ctx.session_id, clock(), settings.auth.session_idle, user_id=ctx.user_id):
        raise ApiError(401, "session_expired", clear_session=True)
    row = db.get(UserSession, ctx.session_id, populate_existing=True)
    if not digest_matches(request.headers.get(CSRF_HEADER) or "", row.csrf_token_hash):
        raise ApiError(403, "csrf_failed")
    db.info["auth_actor"] = (ctx.session_id, ctx.user_id, row.csrf_token_hash, clock, settings.auth.session_idle)
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
    """Same explicit Origin rule as every write in the middleware."""
    if len(request.headers.getlist("origin")) != 1 or not origin_allowed(request.headers.get("origin"), settings.http.allowed_origins):
        raise ApiError(403, "csrf_failed")


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "forbidden")
    return principal
