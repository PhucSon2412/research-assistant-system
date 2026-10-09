"""The single runtime index-request POST: one durable promotion job, nothing else.

Acceptance: Admin User NO KEY UPDATE -> common item UPDATE -> rights UUID ->
publication -> jobs UUID -> version/file UUID (SHARE) -> job/audit DML and
flush -> journal exclusive directly -> exact binding, current actor and fresh
clock after the last SQL -> commit. A live job of the file is returned, never
re-armed. No storage, parser, broker or provider here: the worker finds the
durable job and re-checks the common gate at its own checkpoints.

The attempt owns outcome and executor until settled; commit exceptions are
uncertain (no SELECT inference, replay, retry or IDs). The response is a new
transaction and proof (item SHARE -> resources/job -> journal shared).
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import auth_safety, common_index, publication
from app.api import common_publish
from app.api.errors import ApiError
from app.api.upload_original import _thread
from app.audit import record_event
from app.authz import review_copy_admin_clause
from app.db import get_engine
from app.identity.predicates import lock_users
from app.models import (AccessRights, CommonPublication, IngestionJob, LibraryItem, PaperVersion,
                        SourceFile)
from app.models.enums import (AuditActorType, AuditOutcome, ContentStatus, JobOrigin, JobState,
                              Visibility)

ACTION = "common_copy.index.start"
_attempts: set[asyncio.Task] = set()


class Refusal(ApiError):
    """Business denial under a current proof; the audit carries a code only."""

    def __init__(self, code, reason, item_id):
        super().__init__(409, code)
        self.audit_reason = reason
        self.item_id = item_id


@dataclass
class Outcome:
    # Operation-owned facts: rollback/close/invalidate never overwrite them.
    item_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    file_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    created: bool | None = None
    commit_started: bool = False
    committed: bool = False
    rollback_confirmed: bool = False
    teardown_failed: bool = False


def _current(db, principal, csrf, clock, settings):
    return common_publish._current(db, principal, csrf, clock, settings)


def _release(db, outcome):
    for method in ("rollback", "close"):
        try:
            getattr(db, method)()
            if method == "rollback" and not outcome.commit_started:
                outcome.rollback_confirmed = True
        except BaseException:
            outcome.teardown_failed = True
    if outcome.teardown_failed:
        try:
            db.invalidate()
        except BaseException:
            pass


def _short(function, code):
    """A proof counts only after a clean release on this module's Session."""
    db = Session(get_engine())
    failure = result = None
    released = Outcome()
    try:
        result = function(db)
    except BaseException as exc:
        failure = exc
    finally:
        _release(db, released)
    if failure is not None:
        if isinstance(failure, ApiError) or not isinstance(failure, Exception):
            raise failure
        raise ApiError(503, code) from None
    if released.teardown_failed:
        raise ApiError(503, code)
    return result


def _lock_resources(db, item_id):
    # Same order as revoke after the item; SHARE: nothing here is modified.
    # No tombstone lock (cleanup takes tombstone -> item); tombstone is a predicate.
    for model in (AccessRights, CommonPublication, IngestionJob, PaperVersion, SourceFile):
        key = CommonPublication.item_id if model is CommonPublication else model.id
        db.execute(select(key).where(model.item_id == item_id).order_by(key)
                   .with_for_update(of=model, read=True)).all()


_JOB = (IngestionJob.id, IngestionJob.origin, IngestionJob.scope_visibility, IngestionJob.scope_owner_user_id,
        IngestionJob.item_id, IngestionJob.version_id, IngestionJob.source_file_id,
        IngestionJob.rights_epoch_at_start, IngestionJob.actor_user_id, IngestionJob.idempotency_key,
        IngestionJob.state, IngestionJob.cancel_requested, IngestionJob.finished_at)


def _job_row(db, job_id):
    return db.execute(select(*_JOB).where(IngestionJob.id == job_id)).one_or_none()


def _bound(job, item, source) -> bool:
    """The exact common tuple and epoch of this item now; never re-armed or rewritten."""
    return (job.origin == JobOrigin.PROMOTION.value and job.scope_visibility == Visibility.COMMON.value
            and job.scope_owner_user_id is None and job.item_id == item.id
            and job.version_id == source.version_id and job.source_file_id == source.id
            and job.rights_epoch_at_start == item.rights_epoch and job.finished_at is None)


def _binding(db, principal, outcome, snapshot):
    """After journal: same eligible tuple and the same job row; no locks or DML."""
    item = db.scalar(select(LibraryItem).where(LibraryItem.id == outcome.item_id,
        review_copy_admin_clause(principal)).execution_options(populate_existing=True))
    if item is None:
        raise ApiError(409, "index_binding_changed")
    reason, _, source = common_index.block_reason(db, item)
    if reason is not None or source.id != outcome.file_id or source.version_id != outcome.version_id:
        raise ApiError(409, "index_binding_changed")
    job = _job_row(db, outcome.job_id)
    if job is None or tuple(job) != tuple(snapshot) or not _bound(job, item, source) or job.cancel_requested:
        raise ApiError(409, "index_binding_changed")
    live = db.scalar(select(func.count()).select_from(IngestionJob).where(
        IngestionJob.origin == JobOrigin.PROMOTION.value, IngestionJob.source_file_id == source.id,
        IngestionJob.finished_at.is_(None)))
    if live != 1 or (outcome.created and (job.actor_user_id != principal.user_id
                                          or job.state != JobState.RECEIVED.value)):
        raise ApiError(409, "index_binding_changed")


def accept(principal, raw_id, csrf, clock, settings, stopped, outcome):
    db = Session(get_engine())
    failure = None
    try:
        auth_safety.require_writer(db)
        common_publish._lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)

        def deny(exc):
            auth_safety.require_final(db, writer=True)
            _current(db, principal, csrf, clock, settings)
            raise exc

        try:
            item_id = uuid.UUID(str(raw_id))
        except ValueError:
            deny(ApiError(404, "not_found"))
        item = publication._copy(db, current, item_id, lock=True)
        if item is None:
            deny(ApiError(404, "not_found"))
        _lock_resources(db, item_id)
        reason, _, source = common_index.block_reason(db, item)
        if reason is not None:
            deny(Refusal(common_index.REFUSAL_CODES[reason], reason, item_id))
        outcome.item_id, outcome.version_id, outcome.file_id = item_id, source.version_id, source.id
        running = common_index.live_job(db, source.id)
        if running is not None:
            # A repeated or concurrent request: return the same job, never reset it.
            if not _bound(running, item, source):
                deny(ApiError(409, "index_job_binding_changed"))
            if running.cancel_requested:
                deny(ApiError(409, "index_job_cancelling"))
            outcome.created, outcome.job_id = False, running.id
        else:
            if item.content_status == ContentStatus.INDEXED.value or item.active_index_generation_id is not None:
                deny(Refusal("already_indexed", "already_indexed", item_id))
            previous = common_index.latest_job(db, item_id)
            if previous is not None and previous.state in common_index.EXTRACTION_REJECTED:
                deny(Refusal("extraction_rejected", previous.state, item_id))
            if stopped.is_set():
                raise common_publish.Stopped()
            at = clock()  # after every lock wait
            seq = db.scalar(select(func.count()).select_from(IngestionJob).where(
                IngestionJob.source_file_id == source.id)) + 1
            job = IngestionJob(origin=JobOrigin.PROMOTION.value, actor_user_id=current.user_id,
                source_label=common_index.JOB_LABEL, scope_visibility=Visibility.COMMON.value,
                scope_owner_user_id=None, item_id=item_id, version_id=source.version_id, source_file_id=source.id,
                idempotency_key=f"promotion:{source.id}:{seq}", rights_epoch_at_start=item.rights_epoch,
                state=JobState.RECEIVED.value, created_at=at, updated_at=at)
            db.add(job)
            db.flush()
            record_event(db, action=ACTION, outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.USER,
                         actor_user_id=current.user_id, target_type="library_item", target_id=str(item_id),
                         details={"job_id": str(job.id), "source_file_id": str(source.id)})
            outcome.created, outcome.job_id = True, job.id
        db.flush()  # job unique/FK and audit before journal exclusive
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        snapshot = _job_row(db, outcome.job_id)
        auth_safety.require_final(db, writer=True)  # directly exclusive, never an upgrade
        with db.no_autoflush:
            _binding(db, current, outcome, snapshot)
            _current(db, principal, csrf, clock, settings)  # latest actor/time after all SQL
        if stopped.is_set():
            raise common_publish.Stopped()
        outcome.commit_started = True
        db.commit()
        outcome.committed = True
    except BaseException as exc:
        failure = exc
    finally:
        _release(db, outcome)
    if outcome.commit_started and not outcome.committed:
        raise ApiError(503, "index_start_outcome_uncertain") from None
    if outcome.committed:
        if failure is not None or outcome.teardown_failed:
            raise ApiError(503, "index_start_response_unavailable") from None
        return outcome.item_id
    if isinstance(failure, ApiError):
        raise failure
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    raise ApiError(503, "index_start_unavailable") from None


def refusal_audit(principal, item_id, csrf, clock, settings, reason):
    """Best effort, own writer proof; never retries or replaces the denial."""
    def write(db):
        auth_safety.require_writer(db)
        common_publish._lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)
        record_event(db, action=ACTION, outcome=AuditOutcome.DENIED, actor_type=AuditActorType.USER,
                     actor_user_id=current.user_id, target_type="library_item", target_id=str(item_id),
                     details={"reason_code": reason})
        db.flush()
        auth_safety.require_final(db, writer=True)
        _current(db, principal, csrf, clock, settings)
        db.commit()
    try:
        _short(write, "index_start_unavailable")
    except Exception:
        pass


def respond(principal, item_id, csrf, clock, settings, outcome):
    from app.api.routes import publications

    def read(db):
        common_publish._lock_timeout(db, settings)
        locked = common_publish._lock_item(db, principal, item_id)
        job = None
        if locked is not None:
            common_publish._lock_resources(db, item_id)
            job = db.scalar(select(IngestionJob.id).where(IngestionJob.id == outcome.job_id,
                IngestionJob.item_id == item_id).with_for_update(read=True))
        auth_safety.require_final(db)
        current = _current(db, principal, csrf, clock, settings)
        if locked is None or job is None:
            raise ApiError(409, "resource_scope_changed")
        try:
            dto = publications._copy_out(db, current, item_id, clock())
        except ApiError as exc:
            _current(db, principal, csrf, clock, settings)
            raise ApiError(409, "resource_scope_changed") if exc.status_code == 404 else exc
        publications._read_cleanup_clock([dto], clock)
        # Current job state, whatever the worker, revoke or cleanup did since.
        row = _job_row(db, outcome.job_id)
        if (row is None or row.item_id != outcome.item_id or row.version_id != outcome.version_id
                or row.source_file_id != outcome.file_id or row.origin != JobOrigin.PROMOTION.value
                or row.scope_visibility != Visibility.COMMON.value):
            _current(db, principal, csrf, clock, settings)
            raise ApiError(409, "resource_scope_changed")
        _current(db, principal, csrf, clock, settings)  # after every formatter query
        return {"item": dto, "job": {"id": str(row.id), "state": row.state,
                                     "cancel_requested": row.cancel_requested},
                "created": outcome.created}
    return _short(read, "index_start_response_unavailable")


async def attempt(request, principal, raw_id, csrf, clock, settings, stopped, outcome=None):
    outcome = outcome if outcome is not None else Outcome()
    try:
        try:
            item_id = await _thread(stopped, accept, principal, raw_id, csrf, clock, settings, stopped, outcome)
        except Refusal as exc:
            if not outcome.commit_started:
                await _thread(stopped, refusal_audit, principal, exc.item_id, csrf, clock, settings,
                              exc.audit_reason)
            raise
        if stopped.is_set() or await common_publish._disconnected(request):
            raise ApiError(503, "index_start_response_unavailable")
        result = await _thread(stopped, respond, principal, item_id, csrf, clock, settings, outcome)
        if stopped.is_set() or await common_publish._disconnected(request):
            raise ApiError(503, "index_start_response_unavailable")  # no DTO after a cut-off
        return result
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, "index_start_outcome_uncertain" if outcome.commit_started and not outcome.committed
                       else "index_start_response_unavailable" if outcome.committed
                       else "index_start_unavailable") from None


async def receive(request, principal, raw_id, clock, settings):
    stopped = threading.Event()
    task = asyncio.create_task(attempt(request, principal, raw_id,
        request.headers.get("x-csrf-token") or "", clock, settings, stopped))
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
