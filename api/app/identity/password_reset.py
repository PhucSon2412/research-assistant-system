"""Password reset by single-use link. No password is ever sent anywhere.

Requesting a reset answers identically whether or not the account exists.
Confirming consumes the token once, sets the new verifier and revokes every
session and every other open reset token of the user in one transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import record_event
from app.config import PasswordPolicy
from app.identity.emails import EmailFormatError, normalise_email
from app.identity.errors import InvalidToken
from app.identity.passwords import hash_password, validate_password
from app.identity.sessions import revoke_user_sessions
from app.identity.session_commit import commit_auth
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.models import PasswordResetToken, User
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason, UserStatus


@dataclass(frozen=True)
class IssuedReset:
    user_id: uuid.UUID
    email: str
    token: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"IssuedReset(user_id={self.user_id})"


def _revoke_open_tokens(db: Session, user_id: uuid.UUID, now: datetime) -> None:
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


def request_reset(db: Session, *, email: str, now: datetime, lifetime: timedelta) -> IssuedReset | None:
    """Create a token for an active account; ``None`` otherwise. The caller
    must respond the same way in both cases."""
    try:
        normalised = normalise_email(email)
    except EmailFormatError:
        return None
    user = db.scalar(
        select(User).where(User.email == normalised, User.status == UserStatus.ACTIVE.value).with_for_update(key_share=True)
    )
    if user is None:
        db.rollback()
        return None
    _revoke_open_tokens(db, user.id, now)
    token, digest = generate_token()
    row = PasswordResetToken(user_id=user.id, token_hash=digest, created_at=now, expires_at=now + lifetime)
    db.add(row)
    db.flush()
    record_event(
        db,
        action="auth.password_reset.request",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.SYSTEM,
        target_type="user",
        target_id=str(user.id),
    )
    db.commit()
    return IssuedReset(user_id=user.id, email=user.email, token=token, expires_at=row.expires_at)


def confirm_reset(
    db: Session, *, raw_token: str, new_password: str, now: datetime, policy: PasswordPolicy
) -> uuid.UUID:
    if not plausible_token(raw_token):
        raise InvalidToken()
    digest = hash_token(raw_token)
    open_token = (
        PasswordResetToken.token_hash == digest,
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.revoked_at.is_(None),
        PasswordResetToken.expires_at > now,
    )
    found = db.execute(
        select(PasswordResetToken.id, User.id, User.email)
        .join(User, User.id == PasswordResetToken.user_id)
        .where(*open_token, User.status == UserStatus.ACTIVE.value)
    ).one_or_none()
    if found is None:
        db.rollback()
        raise InvalidToken()
    token_id, user_id, email = found
    validate_password(new_password, policy, email=email)
    verifier = hash_password(new_password)  # slow: outside the locked section
    db.rollback()

    # Lock order user -> token, the same as request_reset, to avoid deadlocks.
    user = db.scalar(
        select(User)
        .where(User.id == user_id, User.status == UserStatus.ACTIVE.value)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    token = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.id == token_id, *open_token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or token is None:
        db.rollback()
        raise InvalidToken()
    token.used_at = now
    user.password_verifier = verifier
    user.password_changed_at = now
    user.updated_at = now
    db.flush()
    _revoke_open_tokens(db, user_id, now)
    revoked = revoke_user_sessions(db, user_id=user_id, reason=SessionRevokeReason.PASSWORD_RESET, now=now)
    record_event(
        db,
        action="auth.password_reset.confirm",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=user_id,
        target_type="user",
        target_id=str(user_id),
        details={"sessions_revoked": revoked},
    )
    commit_auth(db)
    return user_id
