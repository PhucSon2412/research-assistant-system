"""Trusted password verification outside transactions, bounded stale-snapshot refusal."""
from __future__ import annotations
from functools import lru_cache
from sqlalchemy import select, text
from app.audit import record_event
from app.clock import utcnow
from app.auth_safety import account_live, require_writer, require_final
from app.identity.emails import EmailFormatError, normalise_email
from app.identity.errors import InvalidCredentials
from app.identity.passwords import hash_password, verify_password, hasher_for_policy
from app.identity.sessions import issue_session, revoke_session
from app.identity.session_commit import commit_auth
from app.identity.predicates import lock_users, session_live
from app.identity.tokens import hash_token, plausible_token
from app.models import User, UserSession
from app.models.enums import AuditActorType, AuditOutcome, SessionRevokeReason
from app.api.errors import ApiError

SNAPSHOT = ("id", "password_verifier", "password_hash_policy_version", "credential_generation",
            "credential_operation_id", "credential_state", "status_generation", "status_operation_id",
            "status_safety", "status")


@lru_cache
def _dummy_verifier():
    return hash_password("dummy-password-for-timing-only")


def login(db, *, email, password, now, settings, previous_token=None, clock=utcnow):
    require_writer(db)
    try:
        normalised = normalise_email(email)
    except EmailFormatError:
        normalised = None
    if len(password) > settings.password.max_length:
        password = ""
    user = db.scalar(select(User).where(User.email == normalised)) if normalised else None
    snapshot = tuple(getattr(user, key) for key in SNAPSHOT) if user else None
    uid = user.id if user else None
    eligible = account_live(db, uid) if uid else False
    previous = db.execute(select(UserSession.id, UserSession.user_id).where(
        UserSession.token_hash == hash_token(previous_token))).first() if plausible_token(previous_token) else None
    # Hash policy comes only from the migrator-managed DB, not request/config/GUC.
    policy = db.execute(text("SELECT * FROM auth_hash_policies ORDER BY version DESC LIMIT 1")).mappings().one()
    policy = dict(policy)
    db.rollback()  # no read transaction or row locks across Argon2 work
    verified = verify_password(snapshot[1] if snapshot else _dummy_verifier(), password)
    if not verified or not eligible:
        _deny(db, uid, "bad_credentials")
        raise InvalidCredentials()
    hasher = hasher_for_policy(policy)
    upgrade = policy["version"] > snapshot[2] and hasher.check_needs_rehash(snapshot[1])
    new_verifier = hasher.hash(password) if upgrade else None
    require_writer(db)  # catches hold/journal damage during slow work
    lock_users(db, {uid} | ({previous.user_id} if previous else set()))
    locked = db.get(User, uid, populate_existing=True)
    if locked is None or tuple(getattr(locked, key) for key in SNAPSHOT) != snapshot or not account_live(db, uid):
        db.rollback()
        _deny(db, uid, "changed_concurrently")
        raise InvalidCredentials()
    if new_verifier:
        won = db.scalar(text("SELECT ras_auth_rehash(:uid,:old,:gen,:op,:new,:policy,:at)"),
            {"uid": uid, "old": snapshot[1], "gen": locked.credential_generation,
             "op": locked.credential_operation_id, "new": new_verifier, "policy": policy["version"], "at": clock()})
        if not won:
            db.rollback()
            _deny(db, uid, "changed_concurrently")
            raise InvalidCredentials()
        db.refresh(locked)
    issued = issue_session(db, user_id=uid, now=clock(), lifetimes=settings.auth)
    if previous:
        db.execute(text("SELECT ras_auth_session_lock(:uid,:ids)"), {"uid": previous.user_id, "ids": [previous.id]})
        if session_live(db, previous.id, clock(), settings.auth.session_idle, user_id=previous.user_id):
            revoke_session(db, session_id=previous.id, user_id=previous.user_id,
                           reason=SessionRevokeReason.REPLACED, now=clock())
    record_event(db, action="auth.login", outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.USER,
                 actor_user_id=uid, target_type="session", target_id=str(issued.session_id))
    commit_auth(db)
    # A new transaction after a known commit; no cookie before fresh gate/session check.
    try:
        require_final(db)
        if not session_live(db, issued.session_id, clock(), settings.auth.session_idle, user_id=uid):
            raise ApiError(401, "session_expired", clear_session=True)
        db.refresh(locked)
    except Exception:
        db.rollback()
        raise
    return locked, issued


def _deny(db, uid, reason):
    # Never record attempted email/verifier; generic body is not a timing guarantee.
    record_event(db, action="auth.login", outcome=AuditOutcome.DENIED, actor_type=AuditActorType.SYSTEM,
                 target_type="user" if uid else None, target_id=str(uid) if uid else None, details={"reason": reason})
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise ApiError(503, "auth_unavailable") from None
