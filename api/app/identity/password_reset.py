"""Reset writers: latest-head fence, trusted hashing, one journal/receipt batch.

Public requests use normal purpose. Quarantine issuance is an internal writer
for a verified delivery boundary (C3); an email/outbox is not ownership proof.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app import auth_safety
from app.api.errors import ApiError
from app.audit import record_event
from app.clock import utcnow
from app.config import PasswordPolicy
from app.identity.emails import EmailFormatError, normalise_email
from app.identity.errors import InvalidToken
from app.identity.login import SNAPSHOT
from app.identity.passwords import hasher_for_policy, validate_password, verify_password, PasswordPolicyError
from app.identity.predicates import lock_users
from app.identity.sessions import revoke_user_sessions
from app.identity.session_commit import commit_auth, AUTH_PENDING
from app.identity.reset_tokens import lock_open_tokens, end_token, revoke_open_tokens
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.models import PasswordResetToken, User, AuthSafetyHead
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason


@dataclass(frozen=True)
class IssuedReset:
    user_id: uuid.UUID
    email: str = field(repr=False)
    token: str = field(repr=False)
    expires_at: datetime
    token_id: uuid.UUID


def eligible(db, uid, fence, purpose):
    return bool(db.scalar(text("SELECT ras_auth_reset_eligible(:uid,:fence,:purpose)"),
                          {"uid": uid, "fence": fence, "purpose": purpose}))


def token_live(db, tid, uid, at):
    return bool(db.scalar(text("SELECT ras_auth_reset_token_live(:id,:uid,:at)"),
                          {"id": tid, "uid": uid, "at": at}))


def request_reset(db: Session, *, email: str, now: datetime, lifetime: timedelta,
                  clock=utcnow) -> IssuedReset | None:
    return _issue(db, email=email, lifetime=lifetime, clock=clock, purpose="normal")


def _issue(db, *, email, lifetime, clock, purpose):
    # Private staging writer. C3 must authorize verified delivery before exposing
    # quarantine issuance; C2b has no boolean approval flag or public path for it.
    auth_safety.require_writer(db)
    try:
        normalised = normalise_email(email)
    except EmailFormatError:
        db.rollback()
        return None
    uid = db.scalar(select(User.id).where(User.email == normalised))
    db.rollback()
    if uid is None:
        return None
    auth_safety.require_writer(db)
    lock_users(db, [uid])
    user = db.get(User, uid, populate_existing=True)
    head = db.get(AuthSafetyHead, uid, populate_existing=True)
    fence = head.credential_operation_id if head else None
    if not user or not eligible(db, uid, fence, purpose):
        db.rollback()
        return None
    at = clock()
    revoke_open_tokens(db, uid, reason="reset_replaced", now=at)
    tid = uuid.uuid4()
    raw, digest = generate_token()
    db.execute(text("SELECT ras_auth_token_create(:id,:uid,:fence,:purpose,:digest,:at,:expires)"),
               {"id": tid, "uid": uid, "fence": fence, "purpose": purpose, "digest": digest,
                "at": at, "expires": at + lifetime})
    record_event(db, action="auth.password_reset.request", outcome=AuditOutcome.SUCCESS,
                 actor_type=AuditActorType.SYSTEM, target_type="user", target_id=str(uid))
    def guard(tx):
        if not token_live(tx, tid, uid, clock()):
            raise InvalidToken()
    db.info["auth_reset_guard"] = guard
    commit_auth(db)
    auth_safety.require_final(db)
    if not token_live(db, tid, uid, clock()):
        db.rollback()
        raise InvalidToken()
    return IssuedReset(uid, user.email, raw, at + lifetime, tid)


def confirm_reset(db: Session, *, raw_token: str, new_password: str, now: datetime,
                  policy: PasswordPolicy, clock=utcnow) -> uuid.UUID:
    auth_safety.require_writer(db)
    if not plausible_token(raw_token):
        db.rollback()
        raise InvalidToken()
    digest = hash_token(raw_token)
    token = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == digest))
    user = db.get(User, token.user_id) if token else None
    if not user or not token_live(db, token.id, user.id, clock()):
        db.rollback()
        raise InvalidToken()
    snapshot = tuple(getattr(user, key) for key in SNAPSHOT)
    uid, tid, email = user.id, token.id, user.email
    fence, purpose = token.credential_fence_operation_id, token.recovery_purpose
    hash_policy = dict(db.execute(text("SELECT * FROM auth_hash_policies ORDER BY version DESC LIMIT 1")).mappings().one())
    db.rollback()  # including read transaction, before slow verify/hash
    validate_password(new_password, policy, email=email)
    if purpose == "quarantine" and verify_password(snapshot[1], new_password):
        raise PasswordPolicyError("new password must differ from the quarantined password")
    verifier = hasher_for_policy(hash_policy).hash(new_password)
    auth_safety.require_writer(db)
    lock_users(db, [uid])
    locked = db.get(User, uid, populate_existing=True)
    tokens = lock_open_tokens(db, uid)
    target = next((row for row in tokens if row.id == tid and row.token_hash == digest
                   and row.credential_fence_operation_id == fence and row.recovery_purpose == purpose), None)
    if (not locked or tuple(getattr(locked, key) for key in SNAPSHOT) != snapshot
            or target is None or not token_live(db, tid, uid, clock())):
        db.rollback()
        raise InvalidToken()
    at = clock()
    for row in tokens:
        end_token(db, row, kind="consumed" if row is target else "revoked",
                  reason="password_reset" if row is target else "reset_completed", now=at)
    revoked = revoke_user_sessions(db, user_id=uid, reason=SessionRevokeReason.PASSWORD_RESET, now=at)
    def guard(tx):
        row = tx.get(PasswordResetToken, tid, populate_existing=True)
        if (row is None or row.user_id != uid or row.token_hash != digest
                or row.used_at != at or row.revoked_at is not None or row.closure_reason != "password_reset"
                or row.expires_at <= clock() or row.credential_fence_operation_id != fence
                or row.recovery_purpose != purpose or not eligible(tx, uid, fence, purpose)):
            raise InvalidToken()
    db.info["auth_reset_guard"] = guard
    db.info[AUTH_PENDING] = [auth_safety.Change(uid, "credential", "password_reset", at,
                                             verifier=verifier, policy_version=hash_policy["version"])]
    record_event(db, action="auth.password_reset.confirm", outcome=AuditOutcome.SUCCESS,
                 actor_type=AuditActorType.USER, actor_user_id=uid, target_type="user", target_id=str(uid),
                 details={"sessions_revoked": revoked})
    batch = commit_auth(db)
    operation = uuid.UUID(batch[0].record.body["operation_id"])
    auth_safety.require_final(db)
    updated = db.get(User, uid, populate_existing=True)
    if not auth_safety.account_live(db, uid) or updated.credential_operation_id != operation:
        db.rollback()
        raise ApiError(503, "auth_outcome_uncertain")
    return uid
