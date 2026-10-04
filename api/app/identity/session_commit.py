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
from app.identity.errors import InvalidToken

from app.identity.invitation_endings import PENDING as INVITATION_PENDING

PENDING = "session_revocations"
AUTH_PENDING = "auth_changes"


def commit_activity(db: Session) -> None:
    """Touch/CSRF transaction has no journal event; uncertain commit is never success."""
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise ApiError(503, "auth_outcome_uncertain") from None


def _check_actor(db, actor, rows, expected_role=None):
    from app.auth_safety import account_live
    from app.models import User, UserSession
    sid, uid, csrf, clock, idle = actor
    session = db.get(UserSession, sid, populate_existing=True)
    user = db.get(User, uid, populate_existing=True)
    at = clock()
    ours = any(r.id == sid for r in rows)
    if (not session or session.user_id != uid or not user or not account_live(db, uid)
            or session.credential_generation != user.credential_generation
            or session.csrf_token_hash != csrf or session.absolute_expires_at <= at
            or session.last_activity_at <= at - idle or (session.revoked_at is not None and not ours)):
        raise ApiError(401, "session_expired", clear_session=True)
    if expected_role is not None and user.role != expected_role:
        raise ApiError(403, "admin_required")


def commit_auth(db: Session):
    from app.auth_safety import require_final
    rows = db.info.pop(PENDING, [])
    tokens = db.info.pop(TOKEN_PENDING, [])
    invitations = db.info.pop(INVITATION_PENDING, [])
    changes = db.info.pop(AUTH_PENDING, [])
    actor = db.info.pop("auth_actor", None)
    actor_role = db.info.pop("auth_actor_role", None)
    reset_guard = db.info.pop("auth_reset_guard", None)
    if invitations:
        ended_at = invitations[0].accepted_at or invitations[0].revoked_at
    elif tokens:
        ended_at = tokens[0].used_at or tokens[0].revoked_at
    else:
        ended_at = rows[0].revoked_at if rows else None
    # Preserve session-only codes; invitation/token batches name auth endings.
    pending_code = "auth_closure_pending_recovery" if tokens or invitations or changes else "session_revocation_pending_recovery"
    unavailable_code = "auth_journal_unavailable" if tokens or invitations or changes else "session_journal_unavailable"
    batch = []
    commit_attempted = False
    try:
        db.flush()
        require_final(db, writer=True, pending_endings=(invitations, tokens, rows))
        if actor:
            _check_actor(db, actor, rows, actor_role)
        if reset_guard:
            reset_guard(db)
        batch = recovery.record_auth_endings(db, get_settings().recovery.journal_path, tokens, rows,
                                             invitations=invitations, auth_changes=changes)
        commit_attempted = True
        db.commit()
    except recovery.RecoveryError:
        db.rollback()
        raise ApiError(503, "service_recovering") from None
    except ApiError:
        db.rollback()
        raise
    except InvalidToken:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        if batch and all(line.written for line in batch) and not changes:
            try:
                for line in batch:
                    recovery.replay_record(db.get_bind(), line.record, ended_at)
            except Exception:
                raise ApiError(503, pending_code) from None
        raise ApiError(503, "auth_outcome_uncertain" if commit_attempted else "auth_unavailable") from None
    if any(not line.written for line in batch):
        raise ApiError(503, unavailable_code)
    if actor:
        # Success after a known commit still requires a fresh gate/account check.
        require_final(db)
        _check_actor(db, actor, rows, actor_role)
    return batch
