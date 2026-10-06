"""Two original GETs, three independent gates; no DB ownership across I/O.

Resource -> shared journal for reads; User -> resources -> flush -> exclusive
journal for audit. A known audit commit is followed by a new response proof.
The attempt owns the spool until its executor settles or transfers it to ASGI.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import threading
import uuid
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app import auth_safety
from app.api.errors import ApiError
from app.audit import record_event
from app.authz import (Principal, common_original_clause, m2a_private_derivatives_clause,
                      review_grant_clause)
from app.common_original import VerifiedFileResponse, VerifiedOriginal, _slots
from app.db import get_engine
from app.identity.final_caller import final_caller
from app.identity.predicates import lock_users, session_live
from app.models import LibraryItem, PaperVersion, ReviewRequest, SourceFile, User, UserSession
from app.models.enums import AuditActorType, AuditOutcome
from app.promotion import verified_read
from app.storage import is_missing_object

_attempts: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Target:
    file_id: uuid.UUID
    item_id: uuid.UUID
    version_id: uuid.UUID
    storage_key: str
    checksum: str
    byte_size: int
    mime: str
    filename: str | None
    epoch: int
    branch: str
    request_id: uuid.UUID | None
    review_checksum: str | None


@dataclass
class AuditOutcomeState:
    # Never infer the outcome from rollback/close or a later SELECT.
    commit_started: bool = False
    committed: bool = False


def _actor(db, principal, clock, settings, *, review):
    """GET writer actor without CSRF, row locks, journal lock or idle touch."""
    row = db.execute(select(User.role, User.email, User.display_name,
        UserSession.last_activity_at, UserSession.absolute_expires_at)
        .join(UserSession, UserSession.user_id == User.id).where(
            User.id == principal.user_id, UserSession.id == principal.session_id)).one_or_none()
    if row is None or not session_live(db, principal.session_id, clock(), settings.auth.session_idle,
                                      user_id=principal.user_id):
        raise ApiError(401, "session_expired", clear_session=True)
    at = clock()  # after the predicate SQL too
    if row.absolute_expires_at <= at or row.last_activity_at <= at - settings.auth.session_idle:
        raise ApiError(401, "session_expired", clear_session=True)
    if review and row.role != "admin":
        raise ApiError(403, "forbidden")
    return Principal(principal.user_id, row.role, principal.session_id, row.email, row.display_name)


def _caller(db, principal, clock, settings, *, review):
    p = final_caller(db, session_id=principal.session_id, user_id=principal.user_id,
                     clock=clock, idle=settings.auth.session_idle, admin=review)
    return Principal(p.user_id, p.role, p.session_id, p.email, p.display_name)


def _rows(principal, file_id, request_id):
    stmt = select(SourceFile.id, SourceFile.item_id, SourceFile.version_id, SourceFile.storage_key,
        SourceFile.checksum_sha256, SourceFile.byte_size, SourceFile.verified_mime,
        SourceFile.original_filename, LibraryItem.rights_epoch, LibraryItem.visibility)
    stmt = stmt.join(PaperVersion, PaperVersion.id == SourceFile.version_id).join(
        LibraryItem, LibraryItem.id == PaperVersion.item_id).where(
            SourceFile.id == file_id, SourceFile.item_id == LibraryItem.id)
    if request_id is not None:
        return stmt.add_columns(ReviewRequest.id, ReviewRequest.file_checksum_sha256).join(
            ReviewRequest, ReviewRequest.source_file_id == SourceFile.id).where(
                ReviewRequest.id == request_id, review_grant_clause(principal))
    return stmt.where(or_(m2a_private_derivatives_clause(principal), common_original_clause(principal)))


def _target(db, principal, file_id, request_id):
    row = db.execute(_rows(principal, file_id, request_id)).one_or_none()
    if row is None:
        raise ApiError(404, "not_found")
    return Target(*row[:9], "review" if request_id else row.visibility,
                  request_id, row[11] if request_id else None)


def _lock(db, principal, candidate):
    if candidate.request_id is not None:
        db.execute(select(ReviewRequest.id).where(ReviewRequest.id == candidate.request_id)
                   .with_for_update(read=True)).all()
    # Exact SQL gate before taking the private item lock, including review grant.
    allowed = _rows(principal, candidate.file_id, candidate.request_id).with_only_columns(
        LibraryItem.id).correlate(None)
    db.execute(select(LibraryItem.id).where(LibraryItem.id == candidate.item_id,
        LibraryItem.id.in_(allowed)).with_for_update(of=LibraryItem, read=True)).all()
    # File/version rows are immutable in the supported writers; item lock fences
    # cleanup/revoke and request lock fences all review closures. No tombstone lock.


def _session(function, *, outcome=None):
    """Try all teardown paths. A failed teardown never produces a response."""
    db = Session(get_engine())
    result = None
    failure = None
    try:
        result = function(db)
    except BaseException as exc:
        failure = exc
    teardown_failed = False
    try:
        db.rollback()
    except BaseException:
        teardown_failed = True
    try:
        db.close()
    except BaseException:
        teardown_failed = True
    if teardown_failed:
        try:
            db.invalidate()
        except BaseException:
            pass
        failure = failure or ApiError(503, "file_unavailable")
    if outcome is not None and outcome.commit_started and not outcome.committed:
        raise ApiError(503, "download_audit_outcome_uncertain") from None
    if failure is not None:
        if isinstance(failure, (ApiError, ClientDisconnect, asyncio.CancelledError)):
            raise failure
        raise ApiError(503, "file_unavailable") from None
    return result


def proof(principal, file_id, request_id, clock, settings, expected=None):
    def read(db):
        db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))
        try:
            current = _actor(db, principal, clock, settings, review=request_id is not None)
        except ApiError:
            # An invalid account can also have an unconfirmed mirror. The
            # recovery gate remains authoritative on denial, with no new locks.
            _caller(db, principal, clock, settings, review=request_id is not None)
            raise
        try:
            candidate = _target(db, current, file_id, request_id)
        except ApiError:
            _caller(db, principal, clock, settings, review=request_id is not None)
            raise
        _lock(db, current, candidate)
        current = _caller(db, principal, clock, settings, review=request_id is not None)
        try:
            target = _target(db, current, file_id, request_id)
        except ApiError:
            _caller(db, principal, clock, settings, review=request_id is not None)
            raise
        _caller(db, principal, clock, settings, review=request_id is not None)
        # Profile/role changes do not remove an ordinary owner's permission;
        # review alone requires current Admin, enforced by both final callers.
        if target != candidate or (expected is not None and target != expected):
            raise ApiError(404, "not_found")
        return target
    return _session(read)


def _audit(db, principal, target, outcome, **details):
    review = target.branch == "review"
    record_event(db, action="review.original.view" if review else "common_copy.original.download",
        outcome=outcome, actor_type=AuditActorType.USER, actor_user_id=principal.user_id,
        target_type="review_request" if review else "source_file",
        target_id=str(target.request_id if review else target.file_id),
        details=({"source_file_id": str(target.file_id)} if review and outcome == AuditOutcome.SUCCESS else
                 {} if review else {"item_id": str(target.item_id)}) | details)


def audit(principal, target, clock, settings):
    state = AuditOutcomeState()
    def write(db):
        auth_safety.require_writer(db)
        db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))
        lock_users(db, [principal.user_id])
        current = _actor(db, principal, clock, settings, review=target.branch == "review")
        _lock(db, current, target)
        if _target(db, current, target.file_id, target.request_id) != target:
            raise ApiError(404, "not_found")
        _audit(db, current, target, AuditOutcome.SUCCESS)
        db.flush()  # every FK/DML before exclusive journal, no shared upgrade
        auth_safety.require_final(db, writer=True)
        current = _actor(db, principal, clock, settings, review=target.branch == "review")
        if _target(db, current, target.file_id, target.request_id) != target:
            raise ApiError(404, "not_found")
        _actor(db, principal, clock, settings, review=target.branch == "review")
        state.commit_started = True
        db.commit()
        state.committed = True
    _session(write, outcome=state)


def failure_audit(principal, target, clock, settings, outcome, reason):
    # Best effort, IDs/codes only. Never conceal a refusal or retry success audit.
    if target is None or target.branch == "private":
        return
    def write(db):
        auth_safety.require_writer(db)
        db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))
        lock_users(db, [principal.user_id])
        current = _actor(db, principal, clock, settings, review=target.branch == "review")
        _audit(db, current, target, outcome, reason_code=reason)
        db.flush()
        auth_safety.require_final(db, writer=True)
        _actor(db, principal, clock, settings, review=target.branch == "review")
        db.commit()
    try:
        _session(write)
    except Exception:
        pass


def review_denial(principal, file_id, request_id, clock, settings):
    if request_id is None:
        return
    def write(db):
        auth_safety.require_writer(db)
        db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))
        lock_users(db, [principal.user_id])
        current = _actor(db, principal, clock, settings, review=True)
        row = db.execute(select(ReviewRequest.status, ReviewRequest.source_file_id).where(
            ReviewRequest.id == request_id)).one_or_none()
        if row is None:
            return
        reason = ("request_closed" if row.status != "open" else
                  "file_not_in_request" if row.source_file_id != file_id else "grant_inactive")
        record_event(db, action="review.original.view", outcome=AuditOutcome.DENIED,
            actor_type=AuditActorType.USER, actor_user_id=current.user_id,
            target_type="review_request", target_id=str(request_id), details={"reason_code": reason})
        db.flush()
        auth_safety.require_final(db, writer=True)
        _actor(db, principal, clock, settings, review=True)
        db.commit()
    try:
        _session(write)
    except Exception:
        pass


def _verify_spool(spool, target):
    # Flush/seek/read errors must occur before headers. Hash the actual spool
    # too: a short/incorrect temp write cannot turn verified input into a
    # successful partial Content-Length response.
    spool.flush()
    spool.seek(0)
    digest, seen = hashlib.sha256(), 0
    while block := spool.read(64 * 1024):
        seen += len(block)
        if seen > target.byte_size:
            return False
        digest.update(block)
    spool.seek(0)
    return seen == target.byte_size and digest.hexdigest() == target.checksum


def _prepare(principal, file_id, request_id, clock, settings, storage, stopped):
    review = request_id is not None
    try:
        file_id = uuid.UUID(str(file_id))
        request_id = uuid.UUID(str(request_id)) if review else None
    except ValueError:
        # Denial still checks current caller/gate/clock without resources or I/O.
        _session(lambda db: _caller(db, principal, clock, settings, review=review))
        raise ApiError(404, "not_found") from None
    try:
        target = proof(principal, file_id, request_id, clock, settings)
    except ApiError as exc:
        if exc.status_code == 404:
            review_denial(principal, file_id, request_id, clock, settings)
        raise
    if stopped.is_set():
        raise ClientDisconnect()
    slots = _slots(settings.files.verified_download_max_concurrent)
    if not slots.acquire(blocking=False):
        raise ApiError(503, "download_busy")
    verified = VerifiedOriginal(slots)
    try:
        try:
            spool = verified.open_spool()
            intact = verified_read(storage, target.storage_key, target.byte_size, target.checksum, spool)
            intact = intact and _verify_spool(spool, target)
        except Exception as exc:
            missing = is_missing_object(exc)
            failure_audit(principal, target, clock, settings, AuditOutcome.FAILURE,
                          "object_missing" if missing else "storage_unavailable")
            integrity_missing = missing and target.branch != "review"
            raise ApiError(409 if integrity_missing else 503,
                target.branch + "_file_integrity_failed" if integrity_missing else "file_unavailable") from None
        if not intact:
            failure_audit(principal, target, clock, settings, AuditOutcome.FAILURE, "checksum_mismatch")
            raise ApiError(409, target.branch + "_file_integrity_failed")
        if stopped.is_set():
            raise ClientDisconnect()
        try:
            if target.branch != "private":
                audit(principal, target, clock, settings)
            # No precommit target/ORM/proof authorises response after audit commit.
            final = proof(principal, file_id, request_id, clock, settings, target)
        except ApiError as exc:
            if exc.status_code == 404:
                if target.branch == "review":
                    review_denial(principal, file_id, request_id, clock, settings)
                else:
                    failure_audit(principal, target, clock, settings, AuditOutcome.DENIED, "rights_changed")
            raise
        if stopped.is_set():
            raise ClientDisconnect()
        headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(final.filename or "original", safe=""),
            "Content-Length": str(final.byte_size), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        return VerifiedFileResponse(verified, media_type=final.mime, headers=headers)
    except BaseException as failure:
        try:
            verified.close()
        except BaseException:
            pass  # cleanup attempts are exhaustive in the owner; preserve denial/outcome
        if isinstance(failure, (ApiError, ClientDisconnect, asyncio.CancelledError)):
            raise
        raise ApiError(503, "file_unavailable") from None


async def _thread(function, stopped):
    work = asyncio.get_running_loop().run_in_executor(None, contextvars.copy_context().run, function)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        stopped.set()
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not work.cancelled() and work.exception() is None:
            work.result()._verified.close()
        raise


async def receive(request, principal, file_id, clock, settings, storage, request_id=None):
    stopped = threading.Event()
    async def attempt():
        response = await _thread(partial(_prepare, principal, file_id, request_id, clock, settings, storage, stopped), stopped)
        if stopped.is_set():
            response._verified.close()
            raise ClientDisconnect()
        return response
    task = asyncio.create_task(attempt())
    _attempts.add(task)
    def settled(done):
        _attempts.discard(done)
        if not done.cancelled():
            done.exception()
    task.add_done_callback(settled)
    async def disconnect():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                stopped.set()
                return
    watcher = asyncio.create_task(disconnect())
    try:
        await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if stopped.is_set():
            raise ClientDisconnect()
        return await asyncio.shield(task)
    except BaseException:
        stopped.set()
        # Callback retains ownership if the response was allocated concurrently
        # with cancellation, even if the attempt already settled.
        def discard(done):
            if not done.cancelled() and done.exception() is None:
                done.result()._verified.close()
        task.add_done_callback(discard)
        raise
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
