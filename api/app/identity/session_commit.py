"""Commit an auth transaction after journaling its whole session batch.

db.info carries pending rows within this Session only. No cookies or secrets.
All row locks (user -> token -> sessions) precede this final journal step.
Never report login/reset/disable success after a commit exception: replay only
ends sessions, it does not reproduce the rest of that auth transaction.
"""
from sqlalchemy.orm import Session

from app import recovery
from app.api.errors import ApiError
from app.config import get_settings

PENDING = "session_revocations"


def commit_auth(db: Session) -> None:
    rows = db.info.pop(PENDING, [])
    revoked_at = rows[0].revoked_at if rows else None
    batch = []
    commit_attempted = False
    try:
        batch = recovery.record_session_revocations(db, get_settings().recovery.journal_path, rows)
        commit_attempted = True
        db.commit()
    except recovery.RecoveryError:
        db.rollback()
        raise ApiError(503, "service_recovering") from None
    except Exception:
        db.rollback()
        if batch and all(line.written for line in batch):
            try:
                for line in batch:
                    recovery.replay_record(db.get_bind(), line.record, revoked_at)
            except Exception:
                raise ApiError(503, "session_revocation_pending_recovery") from None
        raise ApiError(503, "auth_outcome_uncertain" if commit_attempted else "auth_unavailable") from None
    if any(not line.written for line in batch):
        raise ApiError(503, "session_journal_unavailable")
