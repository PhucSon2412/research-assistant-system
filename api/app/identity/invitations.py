"""Invitation runtime writers. User -> email -> invitation -> FK/audit -> journal.

Anonymous accept never locks an existing User. Slow hashing and delivery have
no transaction. Journal intent is not evidence of account/delivery commit.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import exists, select, text
from sqlalchemy.exc import IntegrityError

from app import auth_safety, journal
from app.api.errors import ApiError
from app.audit import record_event
from app.clock import utcnow
from app.config import PasswordPolicy, get_settings
from app.delivery import MessageKind, OutgoingMessage, build_link
from app.identity.emails import normalise_email
from app.identity.errors import AccountExists, InvalidToken, InvitationConflict, NotFound
from app.identity.invitation_endings import lock_email, unended, end_invitation, lock_invitations
from app.identity.log_safety import protect_auth_logs
from app.identity.passwords import hasher_for_policy, validate_password
from app.identity.predicates import lock_users, session_live
from app.identity.session_commit import AUTH_PENDING, commit_auth, _check_actor
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.models import Invitation, User, AuthChangeReceipt
from app.models.enums import AuditActorType, AuditOutcome


@dataclass(frozen=True)
class _DeliveryBinding:
    invitation_id: uuid.UUID
    token_hash: str = field(repr=False)
    transaction_id: int
    inviter_id: uuid.UUID


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: Invitation = field(repr=False)
    token: str = field(repr=False)
    _binding: _DeliveryBinding = field(repr=False)


def _open(now):
    return (*unended(), Invitation.expires_at > now)


def invitation_status(inv, now):
    if inv.closed_by_recovery_at is not None: return "closed_by_recovery"
    if inv.accepted_at is not None: return "accepted"
    if inv.revoked_at is not None: return "revoked"
    if inv.expires_at <= now: return "expired"
    return "pending"


def _actor(db, uid, sid, csrf, clock, *, write=True):
    protect_auth_logs(db)
    if write:
        auth_safety.require_writer(db)
        lock_users(db, [uid])  # all existing Users before email/invitation
    idle = get_settings().auth.session_idle
    actor = (sid, uid, hash_token(csrf), clock, idle)
    _check_actor(db, actor, [], "admin")
    if write:
        db.info["auth_actor"], db.info["auth_actor_role"] = actor, "admin"
    return actor


def create_invitation(db, *, inviter_id, actor_session_id, csrf_token, email, display_name,
                      now, lifetime, clock=utcnow):
    actor = _actor(db, inviter_id, actor_session_id, csrf_token, clock)
    email = normalise_email(email)
    lock_email(db, email)
    if db.scalar(select(exists().where(User.email == email))):
        raise AccountExists()
    ids = list(db.scalars(select(Invitation.id).where(Invitation.email == email, *unended()).order_by(Invitation.id)))
    lock_invitations(db, ids)
    at = clock()
    _check_actor(db, actor, [], "admin")
    for iid in ids:
        row = db.get(Invitation, iid, populate_existing=True)
        end_invitation(db, row, kind="revoked", reason="invitation_replaced", now=at, actor=actor)
    token, digest = generate_token()
    iid = uuid.uuid4()
    try:
        db.execute(text("SELECT ras_auth_invitation_create(:id,:email,:name,:token,:actor,:sid,:csrf,:at,:expires,:idle)"),
                   {"id": iid, "email": email, "name": display_name, "token": digest, "actor": inviter_id,
                    "sid": actor_session_id, "csrf": actor[2], "at": at, "expires": at + lifetime, "idle": actor[4]})
        inv = db.get(Invitation, iid, populate_existing=True)
        binding = _DeliveryBinding(iid, digest, inv.creation_transaction_id, inviter_id)
        commit_auth(db, postcheck=False)  # own compensation after the known commit
    except IntegrityError:
        db.rollback()
        raise InvitationConflict() from None
    created = CreatedInvitation(inv, token, binding)
    db.rollback()
    try:
        _delivery_check(db, created, actor, clock)
    except ApiError:
        db.rollback()
        _compensate_delivery(db, created, clock)
        raise
    return created


def find_open_invitation(db, raw_token, *, now):
    protect_auth_logs(db)
    if not plausible_token(raw_token): return None
    return db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(raw_token), *_open(now))
                     .execution_options(populate_existing=True))


def inspect_invitation(db, raw_token, *, clock=utcnow):
    protect_auth_logs(db)
    auth_safety.require_final(db)
    row = find_open_invitation(db, raw_token, now=clock())
    if row is None or row.expires_at <= clock(): raise InvalidToken()
    return row


def accept_invitation(db, *, raw_token, password, display_name, now, policy: PasswordPolicy, clock=utcnow):
    protect_auth_logs(db)
    auth_safety.require_writer(db)
    inv = find_open_invitation(db, raw_token, now=clock())
    if inv is None: raise InvalidToken()
    iid, email = inv.id, inv.email
    validate_password(password, policy, email=email)
    hash_policy = dict(db.execute(text("SELECT * FROM auth_hash_policies ORDER BY version DESC LIMIT 1")).mappings().one())
    db.rollback()  # BEFORE Argon2, including the initial read transaction
    verifier = hasher_for_policy(hash_policy).hash(password)
    auth_safety.require_writer(db)
    lock_email(db, email)
    lock_invitations(db, [iid])
    inv = find_open_invitation(db, raw_token, now=clock())
    if inv is None or inv.id != iid or inv.email != email: raise InvalidToken()
    if db.scalar(select(exists().where(User.email == email))): raise InvalidToken()
    if db.scalar(text("SELECT max(version) FROM auth_hash_policies")) != hash_policy["version"]:
        raise InvalidToken()
    uid, at = uuid.uuid4(), clock()
    try:
        db.execute(text("SELECT ras_auth_invitation_prepare(:id,:token,:email,:uid,:name,:v,:p,:at)"),
                   {"id": iid, "token": hash_token(raw_token), "email": email, "uid": uid,
                    "name": display_name, "v": verifier, "p": hash_policy["version"], "at": at})
    except IntegrityError:
        db.rollback()
        raise InvalidToken() from None
    db.refresh(inv)
    from app.identity.invitation_endings import PENDING
    db.info[PENDING] = [inv]
    db.info[AUTH_PENDING] = [
        auth_safety.Change(uid, "credential", "invitation_accept", at, verifier=verifier, policy_version=hash_policy["version"]),
        auth_safety.Change(uid, "status", "invitation_accept", at, decision="active"),
    ]
    def guard(db, *, staged=False):
        fresh = db.get(Invitation, iid, populate_existing=True)
        if (not fresh or fresh.token_hash != hash_token(raw_token) or fresh.email != email
            or fresh.accepted_user_id != uid or fresh.accepted_at != at or fresh.expires_at <= clock()
            or fresh.revoked_at or fresh.closed_by_recovery_at
            or db.scalar(text("SELECT max(version) FROM auth_hash_policies")) != hash_policy["version"]):
            raise InvalidToken()
        if not staged:
            state = journal.read(get_settings().recovery.journal_path)
            if any(r.kind in journal.AUTH_KINDS and str(uid) in
                   (r.body["user_id"], r.body["operation_id"], r.body["generation"]) for r in state.records):
                raise ApiError(503, "auth_uuid_collision")
    db.info["auth_invitation_guard"] = guard
    batch = commit_auth(db)
    operations = {line.record.kind: uuid.UUID(line.record.body["operation_id"]) for line in batch
                  if line.record.kind in journal.AUTH_KINDS and line.record.body["user_id"] == str(uid)}
    db.rollback()
    lock_email(db, email)
    lock_invitations(db, [iid])
    auth_safety.require_final(db)
    fresh = db.get(Invitation, iid, populate_existing=True)
    user = db.get(User, uid, populate_existing=True)
    if (not fresh or fresh.accepted_user_id != uid or fresh.accepted_at != at or not user or user.role != "user"
        or not auth_safety.account_live(db, uid)
        or operations != {journal.CREDENTIAL_TRANSITION: user.credential_operation_id,
                          journal.ACCOUNT_STATUS_DECISION: user.status_operation_id}):
        raise ApiError(503, "auth_outcome_uncertain")
    for kind, op in operations.items():
        receipt = db.get(AuthChangeReceipt, op)
        domain = "credential" if kind == journal.CREDENTIAL_TRANSITION else "status"
        if (not receipt or (receipt.user_id, receipt.domain, receipt.reason_code, receipt.recorded_at, receipt.transaction_id)
            != (uid, domain, "invitation_accept", at, fresh.acceptance_transaction_id)):
            raise ApiError(503, "auth_outcome_uncertain")
    return user


def revoke_invitation(db, *, invitation_id, actor_id, actor_session_id, csrf_token, now, clock=utcnow):
    actor = _actor(db, actor_id, actor_session_id, csrf_token, clock)
    email = db.scalar(select(Invitation.email).where(Invitation.id == invitation_id))
    if email is None: raise NotFound()
    lock_email(db, email)
    lock_invitations(db, [invitation_id])
    row = db.get(Invitation, invitation_id, populate_existing=True)
    if row is None or invitation_status(row, clock()) in ("accepted", "revoked", "closed_by_recovery"):
        raise NotFound()
    _check_actor(db, actor, [], "admin")
    end_invitation(db, row, kind="revoked", reason="admin_revoked", now=clock(), actor=actor)
    commit_auth(db)


def list_invitations(db, *, actor_id, actor_session_id, clock=utcnow, limit=200):
    protect_auth_logs(db)
    rows = list(db.scalars(select(Invitation).order_by(Invitation.created_at.desc()).limit(limit)
                           .execution_options(populate_existing=True)))
    auth_safety.require_final(db)
    if not session_live(db, actor_session_id, clock(), get_settings().auth.session_idle, user_id=actor_id):
        raise ApiError(401, "session_expired", clear_session=True)
    if db.get(User, actor_id, populate_existing=True).role != "admin": raise ApiError(403, "admin_required")
    return rows


def _delivery_check(db, created, actor, clock, *, response=False):
    auth_safety.require_final(db)
    row = db.get(Invitation, created._binding.invitation_id, populate_existing=True)
    b = created._binding
    if (not row or (row.token_hash, row.creation_transaction_id, row.invited_by_user_id)
        != (b.token_hash, b.transaction_id, b.inviter_id)):
        raise ApiError(503, "auth_outcome_uncertain")
    at = clock()
    status = invitation_status(row, at)
    if status == "expired" or (not response and status != "pending"):
        raise ApiError(409, "invitation_unavailable")
    _check_actor(db, actor, [], "admin")
    return row, at


def _compensate_delivery(db, created, clock):
    """Backend-only known-commit context. Keep any winning acceptance/closure."""
    db.rollback()
    auth_safety.require_writer(db)
    b = created._binding
    email = db.scalar(select(Invitation.email).where(Invitation.id == b.invitation_id))
    if email is None: raise ApiError(503, "auth_outcome_uncertain")
    lock_email(db, email)
    lock_invitations(db, [b.invitation_id])
    row = db.get(Invitation, b.invitation_id, populate_existing=True)
    if (row.token_hash, row.creation_transaction_id, row.invited_by_user_id) != (b.token_hash, b.transaction_id, b.inviter_id):
        raise ApiError(503, "auth_outcome_uncertain")
    if row.accepted_at or row.revoked_at or row.closed_by_recovery_at:
        db.rollback()
        return
    at = clock()
    db.execute(text("SELECT ras_auth_invitation_compensate(:id,:token,:tx,:inviter,:at)"),
               {"id": b.invitation_id, "token": b.token_hash, "tx": b.transaction_id, "inviter": b.inviter_id, "at": at})
    db.refresh(row)
    from app.identity.invitation_endings import PENDING
    db.info[PENDING] = [row]
    record_event(db, action="invitation.delivery_compensate", outcome=AuditOutcome.SUCCESS,
                 actor_type=AuditActorType.SYSTEM, target_type="invitation", target_id=str(row.id),
                 details={"reason_code": "delivery_failed"})
    commit_auth(db)


def deliver_invitation(db, created, *, actor_id, actor_session_id, csrf_token, delivery, settings, clock=utcnow):
    if not delivery.enabled:
        _compensate_delivery(db, created, clock)
        raise ApiError(503, "delivery_unavailable")
    actor = (actor_session_id, actor_id, hash_token(csrf_token), clock, settings.auth.session_idle)
    try:
        row, at = _delivery_check(db, created, actor, clock)
        message = OutgoingMessage(MessageKind.INVITATION, row.email,
                                  build_link(settings.http.public_base_url or "", "/invite", created.token), row.expires_at)
        db.rollback()  # no transaction/row/journal lock across adapter I/O
    except ApiError:
        db.rollback()
        _compensate_delivery(db, created, clock)
        raise
    try:
        delivery.send(message, now=at)
    except Exception:
        _compensate_delivery(db, created, clock)
        raise ApiError(503, "delivery_failed") from None
    try:
        row, _ = _delivery_check(db, created, actor, clock, response=True)
        return row
    except ApiError:
        db.rollback()
        _compensate_delivery(db, created, clock)
        raise
