"""Authentication: check email + password, then hand over to session management.

Unknown account, wrong password and disabled account all raise the same
``InvalidCredentials`` after the same amount of Argon2 work, so the response
does not reveal which accounts exist.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.config import Settings
from app.identity.emails import EmailFormatError, normalise_email
from app.identity.errors import InvalidCredentials
from app.identity.passwords import hash_password, needs_rehash, verify_password
from app.identity.sessions import IssuedSession, issue_session, revoke_session
from app.identity.session_commit import commit_auth
from app.identity.tokens import hash_token, plausible_token
from app.models import User, UserSession
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason, UserStatus

AUDIT_ACTION = "auth.login"


@lru_cache
def _dummy_verifier() -> str:
    # Verified against when the account does not exist, to equalise timing.
    return hash_password("dummy-password-for-timing-only")


def login(db: Session, *, email: str, password: str, now: datetime, settings: Settings,
          previous_token: str | None = None) -> tuple[User, IssuedSession]:
    from app.auth_safety import require_writer
    require_writer(db)
    try:
        normalised = normalise_email(email)
    except EmailFormatError:
        normalised = None
    if len(password) > settings.password.max_length:
        # Bounded hashing cost; treated like any other wrong password.
        password = ""

    user = db.scalar(select(User).where(User.email == normalised)) if normalised else None
    if user is None:
        verify_password(_dummy_verifier(), password)
        _deny(db, user=None, reason="bad_credentials")
        raise InvalidCredentials()

    seen_verifier = user.password_verifier
    password_ok = verify_password(seen_verifier, password)
    if not password_ok or user.status != UserStatus.ACTIVE.value:
        _deny(db, user=user, reason="bad_credentials" if not password_ok else "disabled")
        raise InvalidCredentials()
    new_verifier = hash_password(password) if needs_rehash(seen_verifier) else None

    # Re-check under a row lock: a concurrent reset/disable either commits
    # first (we see the new verifier/status and refuse) or waits for us and
    # then revokes the session we are about to create.
    user_id = user.id
    previous = db.execute(select(UserSession.id, UserSession.user_id).where(
        UserSession.token_hash == hash_token(previous_token))).first() if plausible_token(previous_token) else None
    owners = {user_id} | ({previous.user_id} if previous else set())
    db.rollback()
    locked_users = list(db.scalars(select(User).where(User.id.in_(owners)).order_by(User.id)
                                  .with_for_update(key_share=True).execution_options(populate_existing=True)))
    locked = next((row for row in locked_users if row.id == user_id), None)
    if locked is None or locked.password_verifier != seen_verifier or locked.status != UserStatus.ACTIVE.value:
        db.rollback()
        _deny(db, user=user, reason="changed_concurrently")
        raise InvalidCredentials()
    if new_verifier is not None:
        locked.password_verifier = new_verifier
        locked.updated_at = now
    issued = issue_session(db, user_id=locked.id, now=now, lifetimes=settings.auth)
    if previous is not None:
        # All users are locked before either session. Identity is immutable in the DB.
        old = db.scalar(select(UserSession).where(UserSession.id == previous.id).with_for_update()
                        .execution_options(populate_existing=True))
        if old is not None and old.revoked_at is None and old.absolute_expires_at > now \
                and old.last_activity_at > now - settings.auth.session_idle:
            revoke_session(db, session_id=old.id, user_id=old.user_id, reason=SessionRevokeReason.REPLACED, now=now)
    record_event(
        db,
        action=AUDIT_ACTION,
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=locked.id,
        target_type="session",
        target_id=str(issued.session_id),
    )
    commit_auth(db)
    return locked, issued


def _deny(db: Session, *, user: User | None, reason: str) -> None:
    # The attempted email is not recorded: people sometimes type a password
    # into the email field.
    record_event(
        db,
        action=AUDIT_ACTION,
        outcome=AuditOutcome.DENIED,
        actor_type=AuditActorType.SYSTEM,
        target_type="user" if user is not None else None,
        target_id=str(user.id) if user is not None else None,
        details={"reason": reason},
    )
    db.commit()
