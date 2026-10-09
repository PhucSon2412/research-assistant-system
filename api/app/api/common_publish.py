"""Exact POST publish boundary: short proof, unlocked hash, acceptance, new proof.

The attempt owns its frozen common target, slot, executor and outcome until all
threads settle. Publish only lists metadata; download/index/provider gates are
separate. No positive publication event is replayed from the journal.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth_safety
from app.api.errors import ApiError
from app.api.resource_writes import _actor
from app.api.upload_original import _thread
from app.audit import record_event
from app.authz import review_copy_admin_clause
from app.db import get_engine
from app.identity.predicates import lock_users
from app.models import (AccessRights, CommonPublication, DeletionTombstone, LibraryItem,
                        PaperVersion, ReviewRequest, SourceFile)
from app.models.enums import AuditActorType, AuditOutcome
from app.publication import live_duplicate
from app.promotion import COPY_CHUNK_BYTES
from app.storage import is_missing_object

ACTION = "common_copy.publish"
DENIALS = {"already_published": "already_published", "common_copy_revoked": "revoked",
           "rights_not_publishable": "rights_not_publishable",
           "checksum_confirmation_mismatch": "checksum_confirmation_mismatch",
           "duplicate_published_checksum": "duplicate_published_checksum"}


class VerifySlots:
    """Publish-only pool per process; no connection is held while acquiring it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0

    def acquire(self, maximum):
        with self.lock:
            if self.active >= maximum:
                raise ApiError(503, "publication_busy")
            self.active += 1

    def release(self):
        with self.lock:
            self.active -= 1


slots = VerifySlots()
_attempts: set[asyncio.Task] = set()


class Stopped(Exception):
    """Deadline/disconnect/cancellation requested stop; does not kill a thread."""


class PublicationDenial(ApiError):
    """Stable business error plus an internal audit code, never payload text."""

    def __init__(self, reason):
        super().__init__(409, "rights_not_publishable")
        self.audit_reason = reason


@dataclass(frozen=True)
class Target:
    item_id: uuid.UUID
    version_id: uuid.UUID
    file_id: uuid.UUID
    rights_id: uuid.UUID
    review_id: uuid.UUID
    storage_key: str
    byte_size: int
    checksum: str
    epoch: int
    mime: str
    audience: str
    download: bool
    search: bool
    provider: bool
    conditions: str | None


@dataclass
class Outcome:
    # Operation-owned facts: rollback/close/invalidate never overwrite these.
    commit_started: bool = False
    committed: bool = False
    rollback_confirmed: bool = False
    teardown_failed: bool = False


def _lock_timeout(db, settings):
    db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))


def _current(db, principal, csrf, clock, settings):
    current = _actor(db, principal, csrf, clock, settings.auth.session_idle)
    if not current.is_admin:
        raise ApiError(403, "forbidden")
    return current


def _short(function, code):
    """A proof counts only after clean release; try both teardown methods."""
    db = Session(get_engine())
    failure = result = None
    try:
        result = function(db)
    except BaseException as exc:
        failure = exc
    teardown_failed = False
    for method in ("rollback", "close"):
        try:
            getattr(db, method)()
        except BaseException:
            teardown_failed = True
    if teardown_failed:
        try:
            db.invalidate()
        except BaseException:
            pass
    if failure is not None:
        if isinstance(failure, ApiError) or not isinstance(failure, Exception):
            raise failure
        raise ApiError(503, code) from None
    if teardown_failed:
        raise ApiError(503, code)
    return result


def _lock_item(db, principal, item_id, *, writer=False):
    return db.scalar(select(LibraryItem.id).where(LibraryItem.id == item_id,
        review_copy_admin_clause(principal)).with_for_update(of=LibraryItem, read=not writer))


def _lock_resources(db, item_id, *, writer=False):
    # Item -> rights UUID -> version UUID -> file UUID, before journal. No
    # tombstone lock (cleanup takes tombstone -> item); all normal writers
    # take the common item first. No private/review-grant payload is read.
    for model in (AccessRights, PaperVersion, SourceFile):
        db.execute(select(model.id).where(model.item_id == item_id).order_by(model.id)
                   .with_for_update(of=model, read=not writer)).all()


def _target(db, principal, item_id, confirmed_checksum, *, state="pending") -> Target:
    item = db.execute(select(LibraryItem.publication_state, LibraryItem.content_status,
        LibraryItem.active_index_generation_id, LibraryItem.rights_epoch)
        .where(LibraryItem.id == item_id, review_copy_admin_clause(principal))).one_or_none()
    if item is None:
        raise ApiError(404, "not_found")
    # A committed revoke takes precedence over an old target/rights mismatch.
    if item.publication_state == "revoked" or db.scalar(select(DeletionTombstone.id)
            .where(DeletionTombstone.item_id == item_id)) is not None:
        raise ApiError(409, "common_copy_revoked")
    if state == "pending" and item.publication_state == "listed":
        raise ApiError(409, "already_published")
    if (item.publication_state != state or item.content_status != "metadata_only"
            or item.active_index_generation_id is not None):
        raise PublicationDenial("not_metadata_only")
    rights = db.execute(select(AccessRights.id, AccessRights.revoked_at, AccessRights.rights_epoch_at_grant)
                        .where(AccessRights.item_id == item_id)).all()
    if len(rights) != 1:
        raise PublicationDenial("rights_count")
    if rights[0].revoked_at is not None:
        raise PublicationDenial("rights_revoked")
    if rights[0].rights_epoch_at_grant != item.rights_epoch:
        raise PublicationDenial("epoch_mismatch")
    row = db.execute(select(
        LibraryItem.id, PaperVersion.id, SourceFile.id, AccessRights.id,
        AccessRights.origin_review_request_id, SourceFile.storage_key, SourceFile.byte_size,
        SourceFile.checksum_sha256, LibraryItem.rights_epoch, SourceFile.verified_mime,
        AccessRights.scope_audience, AccessRights.permits_original_download,
        AccessRights.permits_fulltext_search, AccessRights.permits_external_processing,
        AccessRights.scope_conditions)
        .join(AccessRights, AccessRights.item_id == LibraryItem.id)
        .join(PaperVersion, PaperVersion.id == AccessRights.version_id)
        .join(SourceFile, SourceFile.id == AccessRights.source_file_id)
        .join(ReviewRequest, ReviewRequest.id == AccessRights.origin_review_request_id)
        .where(LibraryItem.id == item_id, review_copy_admin_clause(principal),
               LibraryItem.owner_user_id.is_(None), AccessRights.revoked_at.is_(None),
               AccessRights.rights_epoch_at_grant == LibraryItem.rights_epoch,
               AccessRights.origin_kind == "review_request", ReviewRequest.status == "approved",
               PaperVersion.item_id == LibraryItem.id, SourceFile.item_id == LibraryItem.id,
               SourceFile.version_id == PaperVersion.id,
               SourceFile.checksum_sha256 == AccessRights.file_checksum_sha256,
               SourceFile.file_source == "review_approval", SourceFile.job_id.is_(None))).one_or_none()
    if row is None:
        raise PublicationDenial("file_mismatch")
    target = Target(*row)
    if confirmed_checksum != target.checksum:
        raise ApiError(409, "checksum_confirmation_mismatch")
    if state == "pending" and live_duplicate(db, target.checksum, item_id) is not None:
        raise ApiError(409, "duplicate_published_checksum")
    return target


def _denial(db, principal, csrf, clock, settings, exc, *, writer=False):
    auth_safety.require_final(db, writer=writer)
    _current(db, principal, csrf, clock, settings)
    raise exc


def caller_denial(principal, csrf, clock, settings):
    def check(db):
        _lock_timeout(db, settings)
        auth_safety.require_final(db)
        _current(db, principal, csrf, clock, settings)
    _short(check, "publication_unavailable")
    raise ApiError(404, "not_found")


def proof(principal, item_id, checksum, csrf, clock, settings):
    def check(db):
        _lock_timeout(db, settings)
        locked = _lock_item(db, principal, item_id)
        if locked is not None:
            _lock_resources(db, item_id)
        auth_safety.require_final(db)
        current = _current(db, principal, csrf, clock, settings)
        if locked is None:
            _denial(db, principal, csrf, clock, settings, ApiError(404, "not_found"))
        try:
            target = _target(db, current, item_id, checksum)
        except ApiError as exc:
            _denial(db, principal, csrf, clock, settings, exc)
        _current(db, principal, csrf, clock, settings)  # after the last resource SQL
        return target
    return _short(check, "publication_unavailable")


def refusal_audit(principal, item_id, csrf, clock, settings, outcome, reason):
    def write(db):
        auth_safety.require_writer(db)
        _lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)
        record_event(db, action=ACTION, outcome=outcome, actor_type=AuditActorType.USER,
                     actor_user_id=current.user_id, target_type="library_item", target_id=str(item_id),
                     details={"reason_code": reason})
        db.flush()
        auth_safety.require_final(db, writer=True)
        _current(db, principal, csrf, clock, settings)
        db.commit()
    try:
        _short(write, "publication_unavailable")
    except Exception:
        pass  # best effort, never retries or replaces the denial


def verify(storage, target: Target, stopped):
    """Stream the common original, bounded by its frozen size, with no DB."""
    body = storage.open(target.storage_key)
    digest, seen = hashlib.sha256(), 0
    try:
        for chunk in body.iter_chunks(chunk_size=COPY_CHUNK_BYTES):
            if stopped.is_set():
                raise Stopped()
            seen += len(chunk)
            if seen > target.byte_size:
                return False
            digest.update(chunk)
    finally:
        try:
            body.close()
        except Exception:
            # Best-effort release if close failed before releasing the socket.
            # A release retry never changes the failed verification verdict.
            try:
                body.close()
            except Exception:
                pass
            raise
    return seen == target.byte_size and digest.hexdigest() == target.checksum


def _binding(db, principal, target, checksum, published_at):
    # No new locks/DML under journal; expect THIS transaction's listed tuple.
    if _target(db, principal, target.item_id, checksum, state="listed") != target:
        raise ApiError(409, "rights_not_publishable")
    if db.scalar(select(CommonPublication.item_id).where(
            CommonPublication.item_id == target.item_id, CommonPublication.access_rights_id == target.rights_id,
            CommonPublication.file_checksum_sha256 == target.checksum,
            CommonPublication.verified_byte_size == target.byte_size,
            CommonPublication.published_by_user_id == principal.user_id,
            CommonPublication.published_at == published_at,
            CommonPublication.revoked_at.is_(None))) is None:
        raise ApiError(409, "rights_not_publishable")


def accept(principal, target, checksum, csrf, clock, settings, stopped, outcome):
    db = Session(get_engine())
    failure = None
    try:
        auth_safety.require_writer(db)
        _lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)
        locked = _lock_item(db, current, target.item_id, writer=True)
        if locked is not None:
            _lock_resources(db, target.item_id, writer=True)
        else:
            _denial(db, principal, csrf, clock, settings, ApiError(404, "not_found"), writer=True)
        try:
            if _target(db, current, target.item_id, checksum) != target:
                raise ApiError(409, "rights_not_publishable")
        except ApiError as exc:
            _denial(db, principal, csrf, clock, settings, exc, writer=True)
        if stopped.is_set():
            raise Stopped()
        at = clock()
        db.execute(text("UPDATE library_items SET publication_state='listed',updated_at=:at WHERE id=:i"),
                   {"i": target.item_id, "at": at})
        db.add(CommonPublication(item_id=target.item_id, access_rights_id=target.rights_id,
            file_checksum_sha256=target.checksum, verified_byte_size=target.byte_size,
            published_at=at, published_by_user_id=current.user_id))
        record_event(db, action=ACTION, outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.USER,
                     actor_user_id=current.user_id, target_type="library_item", target_id=str(target.item_id),
                     details={"access_rights_id": str(target.rights_id), "source_file_id": str(target.file_id)})
        db.flush()  # unique index and ALL audit/FKs before journal exclusive
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        auth_safety.require_final(db, writer=True)  # directly exclusive, never an upgrade
        with db.no_autoflush:
            _binding(db, current, target, checksum, at)
            _current(db, principal, csrf, clock, settings)  # latest actor/time after all SQL
        if stopped.is_set():
            raise Stopped()
        outcome.commit_started = True
        db.commit()
        outcome.committed = True
    except BaseException as exc:
        failure = exc
    finally:
        try:
            db.rollback()
            if not outcome.commit_started:
                outcome.rollback_confirmed = True
        except BaseException:
            outcome.teardown_failed = True
        try:
            db.close()
        except BaseException:
            outcome.teardown_failed = True
        if outcome.teardown_failed:
            try:
                db.invalidate()
            except BaseException:
                pass
    if outcome.committed:
        if failure is not None or outcome.teardown_failed:
            raise ApiError(503, "publication_response_unavailable")
        return target.item_id
    if outcome.commit_started:
        raise ApiError(503, "publication_outcome_uncertain")
    if isinstance(failure, IntegrityError):
        # Transaction aborted on unique constraint: fresh caller/gate proof
        # before the business denial; never a SELECT to infer commit success.
        constraint = getattr(getattr(failure.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_common_publications_live_checksum":
            def check(db):
                auth_safety.require_final(db)
                _current(db, principal, csrf, clock, settings)
            _short(check, "publication_unavailable")
            raise ApiError(409, "duplicate_published_checksum") from None
    if isinstance(failure, ApiError):
        raise failure
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    raise ApiError(503, "publication_unavailable") from None


def respond(principal, item_id, csrf, clock, settings):
    from app.api.routes import publications

    def read(db):
        _lock_timeout(db, settings)
        locked = _lock_item(db, principal, item_id)
        if locked is not None:
            _lock_resources(db, item_id)
        auth_safety.require_final(db)
        current = _current(db, principal, csrf, clock, settings)
        if locked is None:
            _denial(db, principal, csrf, clock, settings, ApiError(409, "resource_scope_changed"))
        try:
            dto = publications._copy_out(db, current, item_id, clock())
        except ApiError as exc:
            _denial(db, principal, csrf, clock, settings,
                    ApiError(409, "resource_scope_changed") if exc.status_code == 404 else exc)
        publications._read_cleanup_clock([dto], clock)
        _current(db, principal, csrf, clock, settings)  # after every formatter query
        return {"item": dto}
    return _short(read, "publication_response_unavailable")


async def _disconnected(request):
    try:
        return request is not None and await request.is_disconnected()
    except Exception:
        return True


async def attempt(request, principal, raw_id, checksum, csrf, clock, settings, storage, stopped,
                  outcome: Outcome | None = None):
    outcome = outcome if outcome is not None else Outcome()
    held = False
    try:
        try:
            item_id = uuid.UUID(str(raw_id))
        except ValueError:
            return await _thread(stopped, caller_denial, principal, csrf, clock, settings)

        async def audited(function, *args):
            try:
                return await _thread(stopped, function, *args)
            except ApiError as exc:
                if exc.code in DENIALS and not outcome.commit_started:
                    await _thread(stopped, refusal_audit, principal, item_id, csrf, clock, settings,
                                  AuditOutcome.DENIED, getattr(exc, "audit_reason", DENIALS[exc.code]))
                raise

        target = await audited(proof, principal, item_id, checksum, csrf, clock, settings)
        if stopped.is_set() or await _disconnected(request):
            raise Stopped()
        slots.acquire(settings.files.publication_verify_max_concurrent)
        held = True
        failure = None
        try:
            async with asyncio.timeout(settings.files.publication_verify_deadline_seconds):
                try:
                    intact = await _thread(stopped, verify, storage, target, stopped)
                except Stopped:
                    raise
                except Exception as exc:
                    failure = ((409, "common_file_integrity_failed", "object_missing") if is_missing_object(exc)
                               else (503, "publication_storage_unavailable", "storage_unavailable"))
                else:
                    if not intact:
                        failure = (409, "common_file_integrity_failed", "checksum_mismatch")
        except TimeoutError:
            failure = (503, "publication_verify_timeout", "verify_timeout")
        if failure is not None:
            # A best-effort audit is not authorization for a late denial.
            # Recheck caller/gate/resource after the storage thread settles;
            # a revoke or auth change wins over the obsolete storage verdict.
            await audited(proof, principal, item_id, checksum, csrf, clock, settings)
            await _thread(stopped, refusal_audit, principal, item_id, csrf, clock, settings,
                          AuditOutcome.FAILURE, failure[2])
            raise ApiError(failure[0], failure[1])
        if stopped.is_set() or await _disconnected(request):
            raise Stopped()
        accepted = await audited(accept, principal, target, checksum, csrf, clock, settings, stopped, outcome)
        return await _thread(stopped, respond, principal, accepted, csrf, clock, settings)
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, "publication_response_unavailable" if outcome.committed else
                       "publication_outcome_uncertain" if outcome.commit_started else
                       "publication_unavailable") from None
    finally:
        # _thread waits for real completion even when cancelled; the SDK body
        # is closed in its own thread before the slot can be returned.
        if held:
            slots.release()


async def receive(request, principal, raw_id, checksum, clock, settings, storage):
    stopped = threading.Event()
    task = asyncio.create_task(attempt(request, principal, raw_id, checksum,
        request.headers.get("x-csrf-token") or "", clock, settings, storage, stopped))
    _attempts.add(task)

    def settled(done):
        _attempts.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(settled)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        raise
