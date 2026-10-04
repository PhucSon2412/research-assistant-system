"""Admin account management: list, disable, enable.

Disabling revokes every session and open reset token in the same
transaction; session resolution also requires ``status = 'active'``, so a
disabled user is locked out on their very next request.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.identity.errors import Forbidden, NotFound
from app.identity.sessions import revoke_user_sessions
from app.identity.session_commit import commit_auth
from app.identity.reset_tokens import revoke_open_tokens
from app.models import User
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason, UserStatus
from app import auth_safety
from app.api.errors import ApiError
from app.clock import utcnow
from app.config import get_settings
from app.identity.predicates import lock_users, session_live
from app.identity.tokens import digest_matches
from app.identity.session_commit import AUTH_PENDING
from app.models import AuthChangeReceipt, AuthSafetyHead, RevocationJournalRecord, UserSession


def _initial_status_verified(db: Session, user_id: uuid.UUID) -> bool:
    """Require a committed initial permission, independently of later restrictions.

    The caller holds the User lock. Receipts are immutable; commit_auth still
    checks the real journal/integrity gate after all row locks, before staging.
    Journal intent alone (including a replayed initial decision) is insufficient.
    """
    receipt = AuthChangeReceipt
    record = RevocationJournalRecord
    head = AuthSafetyHead
    return db.scalar(select(receipt.operation_id).join(record,
        (record.auth_operation_id == receipt.operation_id)
        & (record.auth_user_id == receipt.user_id)
        & (record.auth_domain == receipt.domain)
        & (record.auth_generation == receipt.generation)
        & (record.auth_decision == receipt.decision)
        & (record.auth_reason_code == receipt.reason_code)
        & (record.auth_recorded_at == receipt.recorded_at)
    ).join(head, head.user_id == receipt.user_id).where(
        receipt.user_id == user_id,
        receipt.domain == "status",
        receipt.decision == "active",
        receipt.reason_code.in_(("bootstrap", "invitation_accept", "verified_initial_status")),
        record.journal_confirmed.is_(True),
        record.seq <= head.status_seq,
    ).limit(1)) is not None


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at, User.email)))


def set_user_status(
    db: Session, *, user_id: uuid.UUID, actor_id: uuid.UUID, status: UserStatus, now: datetime,
    actor_session_id: uuid.UUID, csrf_token: str, clock=utcnow
) -> User:
    from app.auth_safety import require_writer
    require_writer(db)
    if user_id == actor_id:
        # Avoid an Admin locking themselves (possibly the only Admin) out.
        raise Forbidden()
    # Gather both Users before any token/session/journal lock, including lower target UUID.
    lock_users(db, [actor_id, user_id])
    actor = db.get(User, actor_id, populate_existing=True)
    idle = get_settings().auth.session_idle
    if not session_live(db, actor_session_id, clock(), idle, user_id=actor_id):
        db.rollback()
        raise ApiError(401, "session_expired", clear_session=True)
    if actor is None or actor.role != "admin":
        db.rollback()
        raise ApiError(403, "admin_required")
    session = db.get(UserSession, actor_session_id, populate_existing=True)
    if not digest_matches(csrf_token, session.csrf_token_hash):
        db.rollback()
        raise ApiError(403, "csrf_failed")
    db.info["auth_actor"] = (actor_session_id, actor_id, session.csrf_token_hash, clock, idle)
    db.info["auth_actor_role"] = "admin"
    user = db.get(User, user_id, populate_existing=True)
    if user is None:
        db.rollback()
        raise NotFound()
    if status is UserStatus.ACTIVE and not _initial_status_verified(db, user_id):
        db.rollback()
        raise ApiError(409, "initial_status_verification_required")
    at = clock()
    if status is UserStatus.DISABLED:
        revoke_open_tokens(db, user_id, reason="user_disabled", now=at)
        revoke_user_sessions(db, user_id=user_id, reason=SessionRevokeReason.USER_DISABLED, now=at)
    db.info[AUTH_PENDING] = [auth_safety.Change(user_id, "status",
        "admin_disable" if status is UserStatus.DISABLED else "admin_enable", at, decision=status.value)]
    record_event(
        db,
        action="user.disable" if status is UserStatus.DISABLED else "user.enable",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=actor_id,
        target_type="user",
        target_id=str(user_id),
    )
    batch = commit_auth(db)
    operation = uuid.UUID(batch[0].record.body["operation_id"])
    db.refresh(user)
    if user.status_operation_id != operation or user.status != status.value or user.status_safety != "proven":
        db.rollback()
        raise ApiError(503, "auth_outcome_uncertain")
    return user
