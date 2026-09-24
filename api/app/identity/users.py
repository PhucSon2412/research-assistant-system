"""Admin account management: list, disable, enable.

Disabling revokes every session and open reset token in the same
transaction; session resolution also requires ``status = 'active'``, so a
disabled user is locked out on their very next request.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import record_event
from app.identity.errors import Forbidden, NotFound
from app.identity.sessions import revoke_user_sessions
from app.models import PasswordResetToken, User
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason, UserStatus


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at, User.email)))


def set_user_status(
    db: Session, *, user_id: uuid.UUID, actor_id: uuid.UUID, status: UserStatus, now: datetime
) -> User:
    if user_id == actor_id:
        # Avoid an Admin locking themselves (possibly the only Admin) out.
        raise Forbidden()
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        db.rollback()
        raise NotFound()
    user.status = status.value
    user.updated_at = now
    if status is UserStatus.DISABLED:
        revoke_user_sessions(db, user_id=user_id, reason=SessionRevokeReason.USER_DISABLED, now=now)
        db.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
    record_event(
        db,
        action="user.disable" if status is UserStatus.DISABLED else "user.enable",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=actor_id,
        target_type="user",
        target_id=str(user_id),
    )
    db.commit()
    return user
