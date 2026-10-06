"""Metadata-only consent writer; no file, provider, or auth journal event.

User -> INSERT/FK/audit -> exclusive journal -> known commit. Response uses
intent SHARE -> acknowledgement SHARE -> shared journal on a NEW transaction.
PUT currently locks intent first too; this reader never requests User afterward.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety
from app.api.errors import ApiError
from app.api.resource_writes import _actor
from app.audit import record_event
from app.authz import Principal
from app.config import Settings
from app.identity.predicates import lock_users, session_live
from app.identity.tokens import generate_token, hash_token, digest_matches
from app.models import ProcessingAcknowledgement as Ack, UploadIntent as Intent, User, UserSession
from app.models.enums import AuditActorType, AuditOutcome


@dataclass(frozen=True)
class Binding:
    intent_id: UUID
    acknowledgement_id: UUID
    user_id: UUID
    session_id: UUID
    notice_version: str
    file_kind: str
    filename: str = field(repr=False)
    declared_size: int
    declared_sha256: str = field(repr=False)
    token_hash: str = field(repr=False)
    created_at: datetime
    expires_at: datetime


def _user(db, principal, csrf_token, clock, settings):
    current = _actor(db, principal, csrf_token, clock, settings.auth.session_idle)
    if current.role != "user":
        raise ApiError(403, "forbidden")
    return current


def _declaration(body, settings):
    # Preserve current-User denial BEFORE business metadata errors. Pydantic
    # schema/strict consent validation still occurs before the handler as usual.
    if body.notice_version != settings.upload_notice_version:
        raise ApiError(409, "notice_changed")
    if Path(body.filename).name != body.filename or "\\" in body.filename or any(ord(c) < 32 for c in body.filename):
        raise ApiError(422, "invalid_filename")
    suffixes = (".pdf",) if body.file_kind == "pdf" else (".md", ".markdown")
    if not body.filename.lower().endswith(suffixes):
        raise ApiError(422, "invalid_filename")
    limit = settings.files.pdf_max_bytes if body.file_kind == "pdf" else settings.files.markdown_max_bytes
    if body.byte_size > limit:
        raise ApiError(413, "file_too_large")


def _binding(db: Session, expected: Binding, settings: Settings):
    # Scalars bypass identities, including those populated before commit. No
    # autoflush, locks or writes here: all FK writes precede journal acquisition.
    row = db.execute(select(
        Intent.id, Intent.acknowledgement_id, Intent.user_id, Intent.session_id,
        Intent.notice_version, Intent.file_kind, Intent.filename, Intent.declared_size,
        Intent.declared_sha256, Intent.token_hash, Intent.created_at, Intent.expires_at,
        Intent.used_at, Intent.item_id, Ack.actor_user_id, Ack.session_id.label("ack_session"),
        Ack.notice_version.label("ack_notice"), Ack.acknowledged_at,
    ).join(Ack, Ack.id == Intent.acknowledgement_id).where(Intent.id == expected.intent_id)).one_or_none()
    if (row is None or tuple(row[:12]) != tuple(vars(expected).values())
            or row.used_at is not None or row.item_id is not None
            or (row.actor_user_id, row.ack_session, row.ack_notice, row.acknowledged_at) != (
                expected.user_id, expected.session_id, expected.notice_version, expected.created_at)):
        raise ApiError(409, "upload_intent_changed")
    if row.notice_version != settings.upload_notice_version:
        raise ApiError(409, "notice_changed")
    return row.expires_at


def _check(db, expected, principal, csrf_token, clock, settings):
    if (expected.user_id, expected.session_id) != (principal.user_id, principal.session_id):
        raise ApiError(409, "upload_intent_changed")
    expires = _binding(db, expected, settings)
    row = db.execute(select(User.role, UserSession.csrf_token_hash,
        UserSession.last_activity_at, UserSession.absolute_expires_at)
        .join(UserSession, UserSession.user_id == User.id)
        .where(User.id == principal.user_id, UserSession.id == principal.session_id)).one_or_none()
    if row is None or not session_live(db, principal.session_id, clock(),
            settings.auth.session_idle, user_id=principal.user_id):
        raise ApiError(401, "session_expired", clear_session=True)
    # A single time AFTER every SQL predicate covers BOTH lifetimes.
    at = clock()
    if row.absolute_expires_at <= at or row.last_activity_at <= at - settings.auth.session_idle:
        raise ApiError(401, "session_expired", clear_session=True)
    if not digest_matches(csrf_token, row.csrf_token_hash):
        raise ApiError(403, "csrf_failed")
    if row.role != "user":
        raise ApiError(403, "forbidden")
    if at >= expires:
        raise ApiError(409, "upload_intent_expired")


def create(db: Session, principal: Principal, body, *, csrf_token: str,
           clock: Callable[[], datetime], settings: Settings) -> dict:
    commit_started = committed = False
    try:
        auth_safety.require_writer(db)
        db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))
        lock_users(db, [principal.user_id])
        _user(db, principal, csrf_token, clock, settings)
        _declaration(body, settings)
        at = clock()
        raw, digest = generate_token()
        ack = Ack(actor_user_id=principal.user_id, session_id=principal.session_id,
                  notice_version=body.notice_version, acknowledged_at=at)
        db.add(ack)
        db.flush()
        intent = Intent(token_hash=digest, acknowledgement_id=ack.id, user_id=principal.user_id,
            session_id=principal.session_id, notice_version=body.notice_version, file_kind=body.file_kind,
            filename=body.filename, declared_size=body.byte_size, declared_sha256=body.sha256,
            created_at=at, expires_at=at + settings.files.upload_intent_lifetime)
        db.add(intent)
        db.flush()
        expected = Binding(intent.id, ack.id, principal.user_id, principal.session_id,
            body.notice_version, body.file_kind, body.filename, body.byte_size, body.sha256,
            digest, at, intent.expires_at)
        record_event(db, action="upload.intent.create", outcome=AuditOutcome.SUCCESS,
            actor_type=AuditActorType.USER, actor_user_id=principal.user_id,
            target_type="upload_intent", target_id=str(intent.id),
            details={"notice_version": body.notice_version})
        db.flush()
        auth_safety.require_final(db, writer=True)
        db.expire_all()
        with db.no_autoflush:
            _check(db, expected, principal, csrf_token, clock, settings)
        commit_started = True
        db.commit()
        committed = True
        db.expire_all()
        with db.no_autoflush:
            # Resource locks FIRST. PUT's intent UPDATE cannot pass this read
            # boundary until it ends; no User/FK writes follow the journal.
            db.execute(select(Intent.id).where(Intent.id == expected.intent_id).with_for_update(read=True)).all()
            db.execute(select(Ack.id).where(Ack.id == expected.acknowledgement_id).with_for_update(read=True)).all()
            auth_safety.require_final(db)
            if hash_token(raw) != expected.token_hash:
                raise ApiError(409, "upload_intent_changed")
            _check(db, expected, principal, csrf_token, clock, settings)
        return {"intent_token": raw, "expires_at": expected.expires_at.isoformat()}
    except ApiError:
        if commit_started and not committed:
            raise ApiError(503, "upload_intent_outcome_uncertain") from None
        raise
    except Exception:
        code = ("upload_intent_response_unavailable" if committed else
                "upload_intent_outcome_uncertain" if commit_started else "upload_intent_unavailable")
        raise ApiError(503, code) from None
    finally:
        # Local cleanup does not establish the outcome of a remote commit.
        try:
            db.rollback()
        except Exception:
            db.close()
