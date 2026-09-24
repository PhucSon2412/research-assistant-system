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
from app.identity.sessions import IssuedSession, issue_session
from app.models import User
from app.models.enums import AuditActorType, AuditOutcome, UserStatus

AUDIT_ACTION = "auth.login"


@lru_cache
def _dummy_verifier() -> str:
    # Verified against when the account does not exist, to equalise timing.
    return hash_password("dummy-password-for-timing-only")


def login(db: Session, *, email: str, password: str, now: datetime, settings: Settings) -> tuple[User, IssuedSession]:
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
    locked = db.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None or locked.password_verifier != seen_verifier or locked.status != UserStatus.ACTIVE.value:
        db.rollback()
        _deny(db, user=user, reason="changed_concurrently")
        raise InvalidCredentials()
    if new_verifier is not None:
        locked.password_verifier = new_verifier
        locked.updated_at = now
    issued = issue_session(db, user_id=locked.id, now=now, lifetimes=settings.auth)
    record_event(
        db,
        action=AUDIT_ACTION,
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=locked.id,
        target_type="session",
        target_id=str(issued.session_id),
    )
    db.commit()
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
