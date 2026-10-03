"""Commit auth after journaling its whole invitation/reset-token/session batch.

db.info carries pending rows within this Session only. No cookies or secrets.
All row locks precede journal: invitation email -> invitations, or
user -> reset token -> sessions. Flush FK/audit writes before journal.
Never report login/reset/disable success after a commit exception: replay only
ends invitations/tokens/sessions, it does not reproduce the rest of that auth transaction.
"""
from sqlalchemy.orm import Session

from app import recovery
from app.api.errors import ApiError
from app.config import get_settings
from app.identity.reset_tokens import PENDING as TOKEN_PENDING

from app.identity.invitation_endings import PENDING as INVITATION_PENDING

PENDING = "session_revocations"


def commit_auth(db: Session) -> None:
    from app.auth_safety import require_writer
    require_writer(db)
    rows = db.info.pop(PENDING, [])
    tokens = db.info.pop(TOKEN_PENDING, [])
    invitations = db.info.pop(INVITATION_PENDING, [])
    if invitations:
        ended_at = invitations[0].accepted_at or invitations[0].revoked_at
    elif tokens:
        ended_at = tokens[0].used_at or tokens[0].revoked_at
    else:
        ended_at = rows[0].revoked_at if rows else None
    # Preserve session-only codes; invitation/token batches name auth endings.
    pending_code = "auth_closure_pending_recovery" if tokens or invitations else "session_revocation_pending_recovery"
    unavailable_code = "auth_journal_unavailable" if tokens or invitations else "session_journal_unavailable"
    batch = []
    commit_attempted = False
    try:
        db.flush()
        batch = recovery.record_auth_endings(db, get_settings().recovery.journal_path, tokens, rows, invitations=invitations)
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
