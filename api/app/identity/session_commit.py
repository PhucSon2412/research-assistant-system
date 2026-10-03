"""Commit auth after journaling its whole reset-token and session batch.

db.info carries pending rows within this Session only. No cookies or secrets.
All row locks (user -> token -> sessions) precede this final journal step.
Never report login/reset/disable success after a commit exception: replay only
ends tokens/sessions, it does not reproduce the rest of that auth transaction.
"""
from sqlalchemy.orm import Session

from app import recovery
from app.api.errors import ApiError
from app.config import get_settings
from app.identity.reset_tokens import PENDING as TOKEN_PENDING

PENDING = "session_revocations"


def commit_auth(db: Session) -> None:
    rows = db.info.pop(PENDING, [])
    tokens = db.info.pop(TOKEN_PENDING, [])
    ended_at = (tokens[0].used_at or tokens[0].revoked_at) if tokens else rows[0].revoked_at if rows else None
    # Keep session-only error codes compatible; token/mixed errors name auth endings.
    pending_code = "auth_closure_pending_recovery" if tokens else "session_revocation_pending_recovery"
    unavailable_code = "auth_journal_unavailable" if tokens else "session_journal_unavailable"
    batch = []
    commit_attempted = False
    try:
        batch = recovery.record_auth_endings(db, get_settings().recovery.journal_path, tokens, rows)
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
                    recovery.replay_record(db.get_bind(), line.record, ended_at)
            except Exception:
                raise ApiError(503, pending_code) from None
        raise ApiError(503, "auth_outcome_uncertain" if commit_attempted else "auth_unavailable") from None
    if any(not line.written for line in batch):
        raise ApiError(503, unavailable_code)
