"""Invitation email serialization and pending endings for the common auth batch."""
import hashlib
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import Invitation

PENDING = "invitation_closures"


def lock_email(db: Session, email: str) -> None:
    # A separate advisory namespace; only a digest is used as the lock key.
    key = int.from_bytes(hashlib.sha256(email.encode()).digest()[:4], "big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(0x7261696E, key)))


def unended():
    return (Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
            Invitation.closed_by_recovery_at.is_(None))


def end_invitation(db: Session, row: Invitation, *, kind: str, reason: str, now: datetime) -> None:
    from app.journal import INVITATION_END_REASONS

    if row.accepted_at or row.revoked_at or row.closed_by_recovery_at:
        raise ValueError("invitation_already_ended")
    if reason not in INVITATION_END_REASONS.get(kind, set()):
        raise ValueError("invalid_invitation_ending")
    if kind == "accepted":
        row.accepted_at = now
    else:
        row.revoked_at = now
    row.closure_reason = reason
    db.info.setdefault(PENDING, []).append(row)
