"""Runtime session routines; generation/receipt/head auth is shared with login."""
from __future__ import annotations
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.clock import utcnow
from app.config import AuthLifetimes, get_settings
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.identity.session_commit import PENDING
from app.identity.predicates import lock_users, live_clause, session_live
from app.auth_safety import require_writer, require_final, account_live
from app.api.errors import ApiError
from app.models import User, UserSession
from app.models.enums import SessionRevokeReason


@dataclass(frozen=True)
class IssuedSession:
    session_id: uuid.UUID
    token: str
    csrf_token: str
    absolute_expires_at: datetime

    def __repr__(self):
        return f"IssuedSession(session_id={self.session_id})"


@dataclass(frozen=True)
class SessionContext:
    session_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    email: str
    display_name: str | None
    csrf_token_hash: str
    absolute_expires_at: datetime
    idle_expires_at: datetime


def issue_session(db: Session, *, user_id, now, lifetimes: AuthLifetimes) -> IssuedSession:
    # Caller entry checks integrity before locks, then commits through commit_auth.
    require_writer(db)
    lock_users(db, [user_id])
    if not account_live(db, user_id):
        raise ApiError(401, "session_expired")
    generation = db.scalar(select(User.credential_generation).where(User.id == user_id))
    token, token_hash = generate_token()
    csrf, csrf_hash = generate_token()
    sid = uuid.uuid4()
    expires = now + lifetimes.session_absolute
    db.execute(text("SELECT ras_auth_session_create(:id,:uid,:gen,:token,:csrf,:at,:expires)"),
               {"id": sid, "uid": user_id, "gen": generation, "token": token_hash,
                "csrf": csrf_hash, "at": now, "expires": expires})
    return IssuedSession(sid, token, csrf, expires)


def resolve_session(db: Session, raw_token, *, now, idle: timedelta, clock=utcnow) -> SessionContext | None:
    require_writer(db)
    if not plausible_token(raw_token):
        return None
    row = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw_token)))
    if row is None:
        return None
    lock_users(db, [row.user_id])
    at = clock()
    if not db.scalar(text("SELECT ras_auth_session_activity(:id,:uid,:at,:idle,NULL)"),
                     {"id": row.id, "uid": row.user_id, "at": at, "idle": idle}):
        return None
    require_final(db)
    at = clock()
    if not session_live(db, row.id, at, idle):
        return None
    db.refresh(row)
    user = db.get(User, row.user_id, populate_existing=True)
    return SessionContext(row.id, row.user_id, user.role, user.email, user.display_name,
                          row.csrf_token_hash, row.absolute_expires_at,
                          min(row.last_activity_at + idle, row.absolute_expires_at))


def session_is_live(db, session_id, *, now, idle, clock=None) -> bool:
    # Item locks may already be held: final READ takes no User/session row locks.
    require_final(db)
    return session_live(db, session_id, clock() if clock else now, idle)


def rotate_csrf(db, session_id, *, user_id=None, clock=utcnow, idle=None) -> str:
    if db.info.get("auth_lock_transaction") is not db.get_transaction() or not db.info.get("auth_locked_users"):
        require_writer(db)
    row = db.get(UserSession, session_id, populate_existing=True)
    if row is None or (user_id is not None and row.user_id != user_id):
        raise ApiError(401, "session_expired")
    lock_users(db, [row.user_id])
    csrf, digest = generate_token()
    if not db.scalar(text("SELECT ras_auth_session_activity(:id,:uid,:at,:idle,:csrf)"),
                     {"id": session_id, "uid": row.user_id, "at": clock(),
                      "idle": idle or get_settings().auth.session_idle, "csrf": digest}):
        raise ApiError(401, "session_expired")
    require_final(db)
    if not session_live(db, session_id, clock(), idle or get_settings().auth.session_idle):
        raise ApiError(401, "session_expired")
    return csrf


def revoke_session(db, *, session_id, user_id, reason: SessionRevokeReason, now) -> bool:
    require_writer(db)
    lock_users(db, [user_id])
    if not db.scalar(text("SELECT ras_auth_session_close(:id,:uid,:at,:reason)"),
                     {"id": session_id, "uid": user_id, "at": now, "reason": reason.value}):
        return False
    row = db.get(UserSession, session_id, populate_existing=True)
    db.info.setdefault(PENDING, []).append(row)
    return True


def revoke_user_sessions(db, *, user_id, reason, now, keep_session_id=None) -> int:
    require_writer(db)
    lock_users(db, [user_id])
    stmt = select(UserSession.id).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if keep_session_id is not None:
        stmt = stmt.where(UserSession.id != keep_session_id)
    ids = list(db.scalars(stmt.order_by(UserSession.id)))
    return sum(revoke_session(db, session_id=sid, user_id=user_id, reason=reason, now=now) for sid in ids)


def list_live_sessions(db, *, user_id, now, idle, clock=None) -> list[UserSession]:
    require_final(db)
    now = clock() if clock else now
    return list(db.scalars(select(UserSession).where(UserSession.user_id == user_id, live_clause(now, idle))
                           .order_by(UserSession.created_at.desc()).execution_options(populate_existing=True)))
