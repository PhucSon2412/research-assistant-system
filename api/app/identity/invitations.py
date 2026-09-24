"""Invitation lifecycle: an Admin invites an email, the invitee sets a password.

Accepting always creates a ``user`` account. No request field can choose the
role, so there is no self-elevation path; granting Admin through an
invitation is an open decision (docs/decisions.md).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_event
from app.config import PasswordPolicy
from app.identity.emails import normalise_email
from app.identity.errors import AccountExists, InvalidToken, InvitationConflict, NotFound
from app.identity.passwords import hash_password, validate_password
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.models import Invitation, User
from app.models.enums import AuditActorType, AuditOutcome, UserRole, UserStatus


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: Invitation
    token: str

    def __repr__(self) -> str:
        return f"CreatedInvitation(id={self.invitation.id})"


def _open(now: datetime):
    return (
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > now,
    )


def invitation_status(inv: Invitation, now: datetime) -> str:
    if inv.accepted_at is not None:
        return "accepted"
    if inv.revoked_at is not None:
        return "revoked"
    if inv.expires_at <= now:
        return "expired"
    return "pending"


def create_invitation(
    db: Session,
    *,
    inviter_id: uuid.UUID,
    email: str,
    display_name: str | None,
    now: datetime,
    lifetime: timedelta,
) -> CreatedInvitation:
    email = normalise_email(email)
    if db.scalar(select(exists().where(User.email == email))):
        raise AccountExists()
    # Supersede any earlier open (or expired-but-unused) invitation.
    db.execute(
        update(Invitation)
        .where(Invitation.email == email, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    token, digest = generate_token()
    inv = Invitation(
        email=email,
        display_name=display_name,
        token_hash=digest,
        invited_by_user_id=inviter_id,
        created_at=now,
        expires_at=now + lifetime,
    )
    db.add(inv)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise InvitationConflict() from exc
    record_event(
        db,
        action="invitation.create",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=inviter_id,
        target_type="invitation",
        target_id=str(inv.id),
    )
    db.commit()
    return CreatedInvitation(invitation=inv, token=token)


def find_open_invitation(db: Session, raw_token: str | None, *, now: datetime) -> Invitation | None:
    if not plausible_token(raw_token):
        return None
    return db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(raw_token), *_open(now)))  # type: ignore[arg-type]


def accept_invitation(
    db: Session,
    *,
    raw_token: str,
    password: str,
    display_name: str | None,
    now: datetime,
    policy: PasswordPolicy,
) -> User:
    """Consume the invitation once and create the account atomically.

    Two concurrent requests with the same token serialise on the invitation
    row lock; the loser re-reads it as accepted and gets ``InvalidToken``.
    """
    inv = find_open_invitation(db, raw_token, now=now)
    if inv is None:
        raise InvalidToken()
    validate_password(password, policy, email=inv.email)
    verifier = hash_password(password)  # slow: outside the locked section
    inv_id = inv.id
    db.rollback()

    locked = db.scalar(
        select(Invitation)
        .where(Invitation.id == inv_id, *_open(now))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        db.rollback()
        raise InvalidToken()
    user = User(
        email=locked.email,
        display_name=display_name or locked.display_name,
        role=UserRole.USER.value,
        status=UserStatus.ACTIVE.value,
        password_verifier=verifier,
        password_changed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:  # email taken meanwhile
        db.rollback()
        raise InvalidToken() from exc
    locked.accepted_at = now
    locked.accepted_user_id = user.id
    record_event(
        db,
        action="invitation.accept",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=user.id,
        target_type="invitation",
        target_id=str(locked.id),
    )
    db.commit()
    return user


def revoke_invitation(db: Session, *, invitation_id: uuid.UUID, actor_id: uuid.UUID, now: datetime) -> None:
    result = db.execute(
        update(Invitation)
        .where(Invitation.id == invitation_id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise NotFound()
    record_event(
        db,
        action="invitation.revoke",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=actor_id,
        target_type="invitation",
        target_id=str(invitation_id),
    )
    db.commit()


def list_invitations(db: Session, *, limit: int = 200) -> list[Invitation]:
    return list(db.scalars(select(Invitation).order_by(Invitation.created_at.desc()).limit(limit)))
