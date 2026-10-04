"""Invitation email serialization and pending endings for the common auth batch."""
import hashlib
from datetime import datetime

from sqlalchemy import select, func, text
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


def lock_invitations(db: Session, ids) -> None:
    db.execute(text("SELECT ras_auth_invitation_lock(:ids)"), {"ids": list(ids)})


def end_invitation(db: Session, row: Invitation, *, kind: str, reason: str, now: datetime, actor=None) -> None:
    from app.journal import INVITATION_END_REASONS

    if row.accepted_at or row.revoked_at or row.closed_by_recovery_at:
        raise ValueError("invitation_already_ended")
    if reason not in INVITATION_END_REASONS.get(kind, set()):
        raise ValueError("invalid_invitation_ending")
    if actor is not None:
        if kind != "revoked":
            raise ValueError("acceptance_requires_prepare")
        sid, uid, csrf, clock, idle = actor
        if not db.scalar(text("SELECT ras_auth_invitation_revoke(:id,:uid,:sid,:csrf,:at,:idle,:reason)"),
                         {"id": row.id, "uid": uid, "sid": sid, "csrf": csrf, "at": now,
                          "idle": idle, "reason": reason}):
            raise ValueError("invitation_already_ended")
        db.refresh(row)
    elif db.scalar(text("SELECT to_regprocedure('ras_auth_invitation_lock(uuid[])') IS NOT NULL")):
        raise ValueError("invitation_actor_required")
    elif kind == "accepted":
        row.accepted_at = now
        row.closure_reason = reason
    else:
        row.revoked_at = now
        row.closure_reason = reason
    db.info.setdefault(PENDING, []).append(row)
