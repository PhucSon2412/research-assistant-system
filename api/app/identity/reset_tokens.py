"""Reset token endings, under User -> tokens (UUID) locks, before sessions/journal."""
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import PasswordResetToken

PENDING = "reset_token_closures"


def lock_open_tokens(db: Session, user_id: uuid.UUID) -> list[PasswordResetToken]:
    stmt = select(PasswordResetToken).where(
        PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None),
        PasswordResetToken.revoked_at.is_(None)).order_by(PasswordResetToken.id)
    ids = list(db.scalars(stmt.with_only_columns(PasswordResetToken.id)))
    db.execute(text("SELECT ras_auth_token_lock(:uid,:ids)"), {"uid": user_id, "ids": ids})
    return list(db.scalars(stmt.execution_options(populate_existing=True)))


def end_token(db: Session, row: PasswordResetToken, *, kind: str, reason: str, now: datetime) -> None:
    if row.used_at is not None or row.revoked_at is not None:
        raise ValueError("reset_token_already_ended")
    if kind not in ("consumed", "revoked"):
        raise ValueError("invalid_reset_token_end_kind")
    won = db.scalar(text("SELECT ras_auth_token_close(:id,:uid,:kind,:reason,:at)"),
                    {"id": row.id, "uid": row.user_id, "kind": kind, "reason": reason, "at": now})
    if not won:
        raise ValueError("reset_token_already_ended")
    db.refresh(row)
    db.info.setdefault(PENDING, []).append(row)


def revoke_open_tokens(db: Session, user_id: uuid.UUID, *, reason: str, now: datetime) -> None:
    for row in lock_open_tokens(db, user_id):
        end_token(db, row, kind="revoked", reason=reason, now=now)
