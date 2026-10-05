"""Final read authority on the caller's transaction, after resource locks.

No row locks, activity writes, commit or rollback. The journal shared lock
belongs to the caller until its transaction ends; proof never crosses commit.
"""
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth_safety import require_final
from app.api.errors import ApiError
from app.identity.predicates import session_live
from app.models import User, UserSession


@dataclass(frozen=True)
class FinalCaller:
    user_id: uuid.UUID
    session_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    idle_expires_at: datetime
    absolute_expires_at: datetime
    checked_at: datetime


def final_caller(db: Session, *, session_id: uuid.UUID, user_id: uuid.UUID,
                 clock: Callable[[], datetime], idle: timedelta, admin: bool = False) -> FinalCaller:
    require_final(db)
    # Scalar columns deliberately bypass stale ORM identities and exclude secrets.
    row = db.execute(select(
        User.email, User.display_name, User.role,
        UserSession.last_activity_at, UserSession.absolute_expires_at,
    ).join(UserSession, UserSession.user_id == User.id).where(
        UserSession.id == session_id, User.id == user_id,
    )).one_or_none()
    # Read the clock after lock wait AND profile read; never renew idle expiry.
    at = clock()
    if row is None or not session_live(db, session_id, at, idle, user_id=user_id):
        raise ApiError(401, "session_expired", clear_session=True)
    if admin and row.role != "admin":
        raise ApiError(403, "forbidden")
    return FinalCaller(user_id, session_id, row.email, row.display_name, row.role,
                       row.last_activity_at + idle, row.absolute_expires_at, at)
