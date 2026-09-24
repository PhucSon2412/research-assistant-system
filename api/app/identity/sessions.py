"""Session management: issue, resolve (validate + touch), revoke.

Separate from authentication (``login.py`` checks who you are) and from
authorisation (``app/authz.py`` decides what you may see). Every API request
resolves its session here; nothing is trusted from the web tier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import AuthLifetimes
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.models import User, UserSession
from app.models.enums import SessionRevokeReason, UserStatus


@dataclass(frozen=True)
class IssuedSession:
    session_id: uuid.UUID
    # Raw values go to the browser cookies only; never log or store them.
    token: str
    csrf_token: str
    absolute_expires_at: datetime

    def __repr__(self) -> str:  # keep raw tokens out of accidental logging
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


def issue_session(db: Session, *, user_id: uuid.UUID, now: datetime, lifetimes: AuthLifetimes) -> IssuedSession:
    """Add a session row to the caller's transaction (caller commits)."""
    token, token_hash = generate_token()
    csrf_token, csrf_hash = generate_token()
    row = UserSession(
        user_id=user_id,
        token_hash=token_hash,
        csrf_token_hash=csrf_hash,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + lifetimes.session_absolute,
    )
    db.add(row)
    db.flush()
    return IssuedSession(
        session_id=row.id, token=token, csrf_token=csrf_token, absolute_expires_at=row.absolute_expires_at
    )


def resolve_session(db: Session, raw_token: str | None, *, now: datetime, idle: timedelta) -> SessionContext | None:
    """Validate the session and record activity in one atomic statement.

    Valid iff not revoked, user active, ``now < last_activity_at + idle`` and
    ``now < absolute_expires_at``. Exactly at a boundary the session is
    expired. Caller commits.
    """
    if not plausible_token(raw_token):
        return None
    stmt = (
        update(UserSession)
        .where(
            UserSession.token_hash == hash_token(raw_token),  # type: ignore[arg-type]
            UserSession.revoked_at.is_(None),
            UserSession.absolute_expires_at > now,
            UserSession.last_activity_at > now - idle,
            UserSession.user_id == User.id,
            User.status == UserStatus.ACTIVE.value,
        )
        .values(last_activity_at=now)
        .returning(
            UserSession.id,
            UserSession.user_id,
            UserSession.csrf_token_hash,
            UserSession.absolute_expires_at,
            User.role,
            User.email,
            User.display_name,
        )
        .execution_options(synchronize_session=False)
    )
    row = db.execute(stmt).one_or_none()
    if row is None:
        return None
    return SessionContext(
        session_id=row.id,
        user_id=row.user_id,
        role=row.role,
        email=row.email,
        display_name=row.display_name,
        csrf_token_hash=row.csrf_token_hash,
        absolute_expires_at=row.absolute_expires_at,
        idle_expires_at=min(now + idle, row.absolute_expires_at),
    )


def rotate_csrf(db: Session, session_id: uuid.UUID) -> str:
    """New CSRF token for a live session whose CSRF cookie was lost."""
    csrf_token, csrf_hash = generate_token()
    db.execute(
        update(UserSession)
        .where(UserSession.id == session_id)
        .values(csrf_token_hash=csrf_hash)
        .execution_options(synchronize_session=False)
    )
    return csrf_token


def revoke_session(
    db: Session, *, session_id: uuid.UUID, user_id: uuid.UUID, reason: SessionRevokeReason, now: datetime
) -> bool:
    """Revoke one of ``user_id``'s own sessions. False if not theirs/not live."""
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=reason.value)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def revoke_user_sessions(
    db: Session,
    *,
    user_id: uuid.UUID,
    reason: SessionRevokeReason,
    now: datetime,
    keep_session_id: uuid.UUID | None = None,
) -> int:
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if keep_session_id is not None:
        stmt = stmt.where(UserSession.id != keep_session_id)
    result = db.execute(
        stmt.values(revoked_at=now, revoked_reason=reason.value).execution_options(synchronize_session=False)
    )
    return result.rowcount


def list_live_sessions(
    db: Session, *, user_id: uuid.UUID, now: datetime, idle: timedelta
) -> list[UserSession]:
    return list(
        db.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.absolute_expires_at > now,
                UserSession.last_activity_at > now - idle,
            )
            .order_by(UserSession.created_at.desc())
        )
    )
