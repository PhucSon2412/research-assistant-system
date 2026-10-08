"""POST approve: short proofs, unlocked copy, one coupled transaction, fresh proof.

Lock orders (scoped C2d-5b choice, not a permanent architecture rule):

- proof: request SHARE -> private item SHARE (exact grant) -> journal shared ->
  scalar current Admin/CSRF/target, clock after the last SQL. The Session is
  owned by its thread and closed before any storage I/O.
- acceptance: Admin User NO KEY UPDATE -> request UPDATE -> private item SHARE
  -> common item/version/file/rights + approved closure/audit -> flush ->
  journal exclusive directly -> scalar checks -> mirror/append/confirm ->
  scalar checks -> commit. No savepoint: the mirror binds rows by xmin.
- response: request SHARE -> common item SHARE (common admin) -> journal shared
  -> current Admin/CSRF/DTO, clock after SQL. No private item, grant or User lock.

The attempt owns key, spool, slot, executor threads and outcome until every
thread has settled; cancellation and the copy deadline only ask it to stop. A
commit exception is uncertain: never a success DTO, replay, retry or copy delete.
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety, journal
from app.api import review_writes
from app.api.errors import ApiError
from app.api.resource_writes import _actor
from app.api.upload_original import _thread
from app.audit import record_event
from app.authz import Principal, review_copy_admin_clause, review_grant_clause, review_history_admin_clause
from app.db import get_engine
from app.identity.predicates import lock_users
from app.models import AccessRights, LibraryItem, PaperVersion, ReviewRequest, SourceFile
from app.models.enums import (AuditActorType, AuditOutcome, PublicationState, ReviewStatus, RightsOrigin,
                              Visibility)
from app.models.review import APPROVED_BY_ADMIN
from app.promotion import COMMON_FILE_SOURCE, COPY_CHUNK_BYTES, RecordedRights

ACTION = "review.request.approve"
# Denials audited with IDs/reason only, through their own writer boundary.
DENIALS = {"review_request_closed": "request_closed", "review_grant_inactive": "grant_inactive",
           "checksum_confirmation_mismatch": "checksum_confirmation_mismatch"}


class CopySlots:
    """Per-process copy pool, separate from upload and verified download pools."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0

    def acquire(self, maximum):
        with self.lock:
            if self.active >= maximum:
                raise ApiError(503, "approval_busy")
            self.active += 1

    def release(self):
        with self.lock:
            self.active -= 1


slots = CopySlots()
_attempts: set[asyncio.Task] = set()


class Stopped(Exception):
    """The attempt asked its thread to stop (deadline, disconnect or cancellation)."""


@dataclass(frozen=True)
class Target:
    """Scalar snapshot of the exact grant; never an ORM row across I/O."""

    request_id: uuid.UUID
    item_id: uuid.UUID
    version_id: uuid.UUID
    file_id: uuid.UUID
    requester_user_id: uuid.UUID
    storage_key: str
    byte_size: int
    mime: str
    page_count: int | None
    filename: str | None
    checksum: str  # the request's; the grant requires the file's to match
    epoch: int


# Independent of the SQL routine: exact private tuple, closure marker of THIS
# transaction and the common tuple/rights inserted by it (xmin is DB-minted).
_COUPLED = text("""
SELECT r.id FROM review_requests r
JOIN library_items pi ON pi.id = r.item_id
JOIN paper_versions pv ON pv.id = r.version_id AND pv.item_id = pi.id
JOIN source_files pf ON pf.id = r.source_file_id AND pf.version_id = pv.id AND pf.item_id = pi.id
JOIN access_rights ar ON ar.origin_review_request_id = r.id
JOIN library_items ci ON ci.id = ar.item_id
JOIN paper_versions cv ON cv.id = ar.version_id AND cv.item_id = ci.id
JOIN source_files cf ON cf.id = ar.source_file_id AND cf.version_id = cv.id AND cf.item_id = ci.id
WHERE r.id = :request_id AND r.item_id = :item_id AND r.version_id = :version_id
  AND r.source_file_id = :source_file_id AND r.requester_user_id = :requester
  AND r.closed_by_user_id = :actor AND r.status = 'approved' AND r.close_reason_code = 'approved_by_admin'
  AND r.closed_at = :closed_at AND r.file_checksum_sha256 = :checksum AND r.rejection_note IS NULL
  AND r.rights_epoch_at_submit = :epoch AND r.closure_transaction_id = txid_current()
  AND pi.visibility = 'private' AND pi.owner_user_id = :requester AND pi.rights_epoch = :epoch
  AND pf.storage_key = :private_key AND pf.byte_size = :size AND pf.verified_mime = :mime
  AND pf.checksum_sha256 = :checksum
  AND ar.id = :rights_id AND ci.id = :common_item_id AND cv.id = :common_version_id AND cf.id = :common_file_id
  AND ar.origin_kind = 'review_request' AND ar.reviewer_user_id = :actor AND ar.recorded_at = :closed_at
  AND ar.file_checksum_sha256 = :checksum AND ar.rights_epoch_at_grant = 0
  AND ci.visibility = 'common' AND ci.publication_state = 'pending' AND ci.owner_user_id IS NULL
  AND ci.rights_epoch = 0 AND ci.created_by_user_id = :actor
  AND cf.storage_key = :key AND cf.storage_key <> pf.storage_key AND cf.checksum_sha256 = :checksum
  AND cf.byte_size = :size AND cf.verified_mime = :mime AND cf.file_source = 'review_approval'
  AND cf.job_id IS NULL
  AND ci.xmin = (txid_current() % 4294967296)::text::xid AND cv.xmin = (txid_current() % 4294967296)::text::xid
  AND cf.xmin = (txid_current() % 4294967296)::text::xid AND ar.xmin = (txid_current() % 4294967296)::text::xid
  AND (SELECT count(*) FROM paper_versions v WHERE v.item_id = ci.id) = 1
  AND (SELECT count(*) FROM source_files f WHERE f.item_id = ci.id) = 1
  AND (:mirrored OR NOT EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.review_request_id = r.id))
""")


@dataclass(frozen=True)
class Approval:
    target: Target
    actor_user_id: uuid.UUID
    closed_at: datetime
    common_item_id: uuid.UUID
    common_version_id: uuid.UUID
    common_file_id: uuid.UUID
    rights_id: uuid.UUID
    key: str

    @property
    def request_id(self):
        return self.target.request_id

    def coupled(self, db, *, mirrored):
        t = self.target
        return db.scalar(_COUPLED, dict(
            request_id=t.request_id, item_id=t.item_id, version_id=t.version_id, source_file_id=t.file_id,
            requester=t.requester_user_id, actor=self.actor_user_id, closed_at=self.closed_at,
            checksum=t.checksum, epoch=t.epoch, private_key=t.storage_key, size=t.byte_size, mime=t.mime,
            rights_id=self.rights_id, common_item_id=self.common_item_id,
            common_version_id=self.common_version_id, common_file_id=self.common_file_id, key=self.key,
            mirrored=mirrored))

    def waiting(self, db):
        # review_writes._integrity subtracts exactly this staged closure only.
        return self.coupled(db, mirrored=False)

    def body(self):
        t = self.target
        return dict(request_id=str(t.request_id), item_id=str(t.item_id), version_id=str(t.version_id),
                    source_file_id=str(t.file_id), status=ReviewStatus.APPROVED.value,
                    reason_code=APPROVED_BY_ADMIN, closed_at=self.closed_at.isoformat())


@dataclass
class Outcome:
    # Owned by the attempt; teardown, close or cancellation never rewrites it.
    approval: Approval | None = None
    record: journal.Record | None = None
    append_started: bool = False
    written: bool = False
    commit_started: bool = False
    committed: bool = False
    rollback_confirmed: bool = False
    teardown_failed: bool = False


def rights_of(body) -> RecordedRights:
    """The Admin's own findings; nothing is copied from the User's declaration."""
    return RecordedRights(
        title=body.title, verified_source=body.verified_source, verified_source_url=body.verified_source_url,
        license_url=body.license_url, evidence_note=body.evidence_note, checked_on=body.checked_on,
        audience=body.scope.audience, permits_original_download=body.scope.original_download,
        permits_fulltext_search=body.scope.fulltext_search,
        permits_external_processing=body.scope.external_processing, scope_conditions=body.scope.conditions)


def _lock_timeout(db, settings):
    db.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))


def _current(db, principal, csrf, clock, settings) -> Principal:
    # Scalar session/User/receipt/CSRF; TTL against a clock read after the SQL.
    current = _actor(db, principal, csrf, clock, settings.auth.session_idle)
    if not current.is_admin:
        raise ApiError(403, "forbidden")
    return current


def _validate(body, at: datetime) -> None:
    if body.license_url is None and body.evidence_note is None:
        raise ApiError(422, "rights_evidence_required")
    if body.scope.external_processing and not body.scope.fulltext_search:
        raise ApiError(422, "rights_scope_inconsistent")
    # One day of slack for an Admin whose local date is ahead of UTC, as in the DB CHECK.
    if body.checked_on > at.astimezone(UTC).date() + timedelta(days=1):
        raise ApiError(422, "checked_on_in_future")


def _target(db, principal, rid) -> Target | None:
    row = db.execute(select(
        ReviewRequest.id, LibraryItem.id, PaperVersion.id, SourceFile.id, ReviewRequest.requester_user_id,
        SourceFile.storage_key, SourceFile.byte_size, SourceFile.verified_mime, SourceFile.page_count,
        SourceFile.original_filename, ReviewRequest.file_checksum_sha256, LibraryItem.rights_epoch)
        .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
        .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
        .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
        .where(ReviewRequest.id == rid, review_grant_clause(principal))).one_or_none()
    return None if row is None else Target(*row)


def _granted_items(principal, rid):
    return (select(ReviewRequest.item_id)
            .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
            .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
            .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
            .where(ReviewRequest.id == rid, review_grant_clause(principal)).correlate(None))


def _lock_private_item(db, principal, rid):
    # Only the exact grant can lock the private item; closed history locks nothing private.
    db.execute(select(LibraryItem.id).where(LibraryItem.id.in_(_granted_items(principal, rid)))
               .with_for_update(of=LibraryItem, read=True)).all()


def _status(db, current, rid):
    return db.scalar(select(ReviewRequest.status).where(ReviewRequest.id == rid,
                                                        review_history_admin_clause(current)))


def _short(function, code):
    """Run one short proof/writer on its own Session; never a 500 from teardown.

    Rollback and close are both attempted; if either fails the connection is
    invalidated and the attempt stops with a controlled error: a proof only
    counts once its Session was released cleanly. A denial raised by the proof
    itself is kept; non-Exception BaseExceptions are re-raised unchanged.
    """
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
            db.invalidate()  # release/discard the connection even if close failed
        except BaseException:
            pass
    if failure is not None:
        if isinstance(failure, ApiError) or not isinstance(failure, Exception):
            raise failure
        raise ApiError(503, code) from None
    if teardown_failed:
        raise ApiError(503, code)
    return result


def caller_denial(principal, csrf, clock, settings):
    """Malformed IDs still face the current caller/gate/clock, with no resource lock."""
    def check(db):
        _lock_timeout(db, settings)
        auth_safety.require_final(db)
        _current(db, principal, csrf, clock, settings)
    _short(check, "approval_unavailable")
    raise ApiError(404, "not_found")


def proof(principal, rid, body, csrf, clock, settings, expected: Target | None = None) -> Target:
    """Short proof on its own Session; the connection is returned before any I/O."""
    def check(db):
        _lock_timeout(db, settings)
        db.execute(select(ReviewRequest.id).where(ReviewRequest.id == rid,
            review_history_admin_clause(principal)).with_for_update(read=True)).all()
        _lock_private_item(db, principal, rid)
        auth_safety.require_final(db)
        current = _current(db, principal, csrf, clock, settings)

        def deny(status, code):
            _current(db, principal, csrf, clock, settings)  # auth loss wins over a late denial
            raise ApiError(status, code)

        try:
            _validate(body, clock())
        except ApiError as exc:
            deny(exc.status_code, exc.code)
        status = _status(db, current, rid)
        if status is None:
            deny(404, "not_found")
        if status != ReviewStatus.OPEN.value:
            deny(409, "review_request_closed")
        target = _target(db, current, rid)
        if target is None or (expected is not None and target != expected):
            deny(409, "review_grant_inactive")
        if body.file_checksum_sha256 != target.checksum:
            deny(409, "checksum_confirmation_mismatch")
        _current(db, principal, csrf, clock, settings)  # clock AFTER every SQL
        return target
    return _short(check, "approval_unavailable")


def refusal_audit(principal, rid, csrf, clock, settings, outcome, reason):
    """Best effort, IDs/code only: User -> audit/FK flush -> exclusive journal -> actor."""
    def write(db):
        auth_safety.require_writer(db)
        _lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)
        record_event(db, action=ACTION, outcome=outcome, actor_type=AuditActorType.USER,
                     actor_user_id=current.user_id, target_type="review_request", target_id=str(rid),
                     details={"reason_code": reason})
        db.flush()
        auth_safety.require_final(db, writer=True)
        _current(db, principal, csrf, clock, settings)
        db.commit()
    try:
        _short(write, "approval_unavailable")
    except Exception:
        pass  # never conceals the refusal, never retried


# --- storage, outside every DB session ------------------------------------------

def open_spool():
    return tempfile.TemporaryFile(prefix="ras-approval-")


def _digest(body, size, stopped, spool=None):
    digest, seen = hashlib.sha256(), 0
    try:
        for chunk in body.iter_chunks(chunk_size=COPY_CHUNK_BYTES):
            if stopped.is_set():
                raise Stopped()
            seen += len(chunk)
            if seen > size:
                return None
            digest.update(chunk)
            if spool is not None:
                spool.write(chunk)
    finally:
        body.close()
    return digest.hexdigest() if seen == size else None


class SpoolMismatch(OSError):
    """The temp spool does not hold the verified bytes: a local storage fault."""


def read_source(storage, target: Target, spool, stopped) -> bool:
    """Private original -> temp spool, bounded by size; False only for wrong source bytes.

    The actual spool is hashed again: a short or wrong temp write is a local
    storage fault (raised), never an integrity verdict on the owner's original.
    """
    if _digest(storage.open(target.storage_key), target.byte_size, stopped, spool) != target.checksum:
        return False
    spool.flush()
    spool.seek(0)
    digest, seen = hashlib.sha256(), 0
    while block := spool.read(COPY_CHUNK_BYTES):
        seen += len(block)
        if seen > target.byte_size:
            break
        digest.update(block)
    if seen != target.byte_size or digest.hexdigest() != target.checksum:
        raise SpoolMismatch("approval spool mismatch")
    spool.seek(0)
    return True


def store_copy(storage, key, target: Target, spool, stopped) -> bool:
    """Put the verified spool at the attempt's own key, then read the copy back."""
    if stopped.is_set():
        raise Stopped()
    storage.put(key, spool, target.mime)
    if stopped.is_set():
        raise Stopped()
    return _digest(storage.open(key), target.byte_size, stopped) == target.checksum


def discard(storage, key):
    try:
        storage.delete(key)
    except Exception:
        pass  # unreferenced, unreachable; reconcile_originals sweeps it later


def close_spool(spool):
    try:
        spool.close()
    except Exception:
        pass  # temp file; never replaces the attempt's outcome


# --- acceptance and response ---------------------------------------------------

def _check(db, approval: Approval, *, mirrored):
    if approval.coupled(db, mirrored=mirrored) != approval.request_id:
        raise ApiError(409, "review_grant_inactive")


def _gate(db, settings, pending=None):
    return review_writes._gate(db, settings, pending)


def accept(principal, target: Target, body, rights: RecordedRights, csrf, clock, settings, key, stopped,
           outcome: Outcome) -> Approval:
    db = Session(get_engine())
    failure = None
    try:
        auth_safety.require_writer(db)
        _lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)

        def deny(status, code):
            _gate(db, settings)  # exclusive journal directly; no shared upgrade
            _current(db, principal, csrf, clock, settings)
            raise ApiError(status, code)

        rr = db.scalar(select(ReviewRequest).where(ReviewRequest.id == target.request_id,
            review_history_admin_clause(current)).with_for_update(of=ReviewRequest))
        if rr is None:
            deny(404, "not_found")
        if rr.status != ReviewStatus.OPEN.value:
            deny(409, "review_request_closed")
        _lock_private_item(db, current, target.request_id)
        # Pinned scalars: never take a fresh target and keep bytes read for the old one.
        if _target(db, current, target.request_id) != target:
            deny(409, "review_grant_inactive")
        if body.file_checksum_sha256 != target.checksum:
            deny(409, "checksum_confirmation_mismatch")
        now = clock()
        try:
            _validate(body, now)
        except ApiError as exc:
            deny(exc.status_code, exc.code)
        if stopped.is_set():
            raise Stopped()

        item = LibraryItem(visibility=Visibility.COMMON.value, owner_user_id=None,
                           publication_state=PublicationState.PENDING.value, title=rights.title,
                           rights_epoch=0, created_by_user_id=current.user_id, created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        # Only the Admin-checked source URL describes the common version.
        version = PaperVersion(item_id=item.id, source_url=rights.verified_source_url, received_at=now,
                               created_at=now)
        db.add(version)
        db.flush()
        common_file = SourceFile(item_id=item.id, version_id=version.id, storage_key=key,
                                 verified_mime=target.mime, byte_size=target.byte_size,
                                 page_count=target.page_count, checksum_sha256=target.checksum,
                                 original_filename=target.filename, file_source=COMMON_FILE_SOURCE,
                                 created_at=now)
        db.add(common_file)
        db.flush()
        recorded = AccessRights(
            item_id=item.id, version_id=version.id, source_file_id=common_file.id,
            file_checksum_sha256=target.checksum, origin_kind=RightsOrigin.REVIEW_REQUEST.value,
            origin_review_request_id=rr.id, verified_source=rights.verified_source,
            verified_source_url=rights.verified_source_url, license_url=rights.license_url,
            evidence_note=rights.evidence_note, checked_on=rights.checked_on, reviewer_user_id=current.user_id,
            recorded_at=now, scope_audience=rights.audience.value,
            permits_original_download=rights.permits_original_download,
            permits_fulltext_search=rights.permits_fulltext_search,
            permits_external_processing=rights.permits_external_processing,
            scope_conditions=rights.scope_conditions, rights_epoch_at_grant=0)
        db.add(recorded)
        rr.status = ReviewStatus.APPROVED.value
        rr.closed_at = now
        rr.closed_by_user_id = current.user_id
        rr.close_reason_code = APPROVED_BY_ADMIN
        db.flush()
        record_event(db, action=ACTION, outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.USER,
                     actor_user_id=current.user_id, target_type="review_request", target_id=str(rr.id),
                     details={"common_item_id": str(item.id), "source_file_id": str(common_file.id),
                              "access_rights_id": str(recorded.id)})
        db.flush()  # every DML/FK/audit before the exclusive journal
        outcome.approval = Approval(target, current.user_id, now, item.id, version.id, common_file.id,
                                    recorded.id, key)
        state = _gate(db, settings, outcome.approval)
        with db.no_autoflush:
            _check(db, outcome.approval, mirrored=False)
            _current(db, principal, csrf, clock, settings)
            if stopped.is_set():
                raise Stopped()
            outcome.record = journal.next_record(state.head, journal.REVIEW_CLOSURE, outcome.approval.body())
            db.execute(text("SELECT ras_auth_review_approval_mirror(:line)"), {"line": outcome.record.line})
            outcome.append_started = True
            try:
                journal.append(settings.recovery.journal_path, state, [outcome.record])
            except (OSError, journal.JournalError):
                # Only append/fsync errors enter the fail-closed commit. Actor
                # and binding are still rechecked below, without the normal
                # gate that would hide this transaction's own false mirror.
                pass
            else:
                outcome.written = True
                db.execute(text("SELECT ras_auth_review_approval_confirm(:line)"), {"line": outcome.record.line})
                _gate(db, settings)
            _check(db, outcome.approval, mirrored=True)
            _current(db, principal, csrf, clock, settings)
            if stopped.is_set():
                raise Stopped()
        outcome.commit_started = True
        db.commit()
        outcome.committed = True
    except BaseException as exc:
        failure = exc
    finally:
        # Neither rollback nor close can rewrite a started/successful commit.
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
                db.invalidate()  # release/discard the connection even if close failed
            except BaseException:
                pass
    if outcome.committed:
        if failure is not None or outcome.teardown_failed:
            raise ApiError(503, "approval_response_unavailable")
        if not outcome.written:
            # Approved and committed; the journal lacks it, so the gate stays closed.
            raise ApiError(503, "review_journal_unavailable")
        return outcome.approval
    if outcome.commit_started:
        raise ApiError(503, "approval_outcome_uncertain")
    if isinstance(failure, ApiError):
        raise failure
    if outcome.append_started:
        raise ApiError(503, "review_closure_pending_recovery")
    raise ApiError(503, "approval_unavailable")


def respond(principal, approval: Approval, csrf, clock, settings) -> dict:
    """New transaction after a known commit; the precommit proof authorises nothing."""
    from app.api.routes import reviews
    rid = approval.request_id

    def read(db):
        _lock_timeout(db, settings)
        db.execute(select(ReviewRequest.id).where(ReviewRequest.id == rid,
            review_history_admin_clause(principal)).with_for_update(read=True)).all()
        db.execute(select(LibraryItem.id).where(LibraryItem.id == approval.common_item_id,
            review_copy_admin_clause(principal)).with_for_update(of=LibraryItem, read=True)).all()
        auth_safety.require_final(db)
        current = _current(db, principal, csrf, clock, settings)
        row = db.execute(select(ReviewRequest.id, ReviewRequest.status, ReviewRequest.submitted_at,
            ReviewRequest.closed_at, ReviewRequest.close_reason_code, ReviewRequest.closed_by_user_id)
            .where(ReviewRequest.id == rid, review_history_admin_clause(current))).one_or_none()
        if row is None or (row.status, row.close_reason_code, row.closed_at, row.closed_by_user_id) != (
                ReviewStatus.APPROVED.value, APPROVED_BY_ADMIN, approval.closed_at, approval.actor_user_id):
            _current(db, principal, csrf, clock, settings)
            raise ApiError(409, "resource_scope_changed")
        # The copy may since be published or revoked: the ID only through the current gate.
        common = db.scalar(select(LibraryItem.id).join(AccessRights, AccessRights.item_id == LibraryItem.id)
            .where(LibraryItem.id == approval.common_item_id, AccessRights.id == approval.rights_id,
                   AccessRights.origin_review_request_id == rid, review_copy_admin_clause(current)))
        dto = reviews._admin_out(SimpleNamespace(**row._mapping), None, common)
        _current(db, principal, csrf, clock, settings)  # clock AFTER the DTO SQL
        return {"request": dto}
    return _short(read, "approval_response_unavailable")


# --- the attempt ------------------------------------------------------------------

async def _disconnected(request):
    try:
        return request is not None and await request.is_disconnected()
    except Exception:
        return True


async def attempt(request, principal, raw_id, body, csrf, clock, settings, storage, stopped,
                  outcome: Outcome | None = None):
    outcome = outcome if outcome is not None else Outcome()
    try:
        rid = uuid.UUID(str(raw_id))
    except ValueError:
        rid = None
    if rid is None:
        try:
            await _thread(stopped, caller_denial, principal, csrf, clock, settings)
        except ApiError:
            raise
        except Exception:
            raise ApiError(503, "approval_unavailable") from None
        raise ApiError(404, "not_found")
    rights = rights_of(body)

    async def audited(function, *args):
        try:
            return await _thread(stopped, function, *args)
        except ApiError as exc:
            if exc.code in DENIALS and not outcome.append_started:
                await _thread(stopped, refusal_audit, principal, rid, csrf, clock, settings,
                              AuditOutcome.DENIED, DENIALS[exc.code])
            raise

    try:
        # Before any slot, storage or mutation: a proof that did not finish
        # cleanly (including its Session release) ends the attempt here.
        target = await audited(proof, principal, rid, body, csrf, clock, settings)
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, "approval_unavailable") from None
    slots.acquire(settings.files.approval_copy_max_concurrent)
    key = f"originals/{uuid.uuid4().hex}"
    spool = None
    put_started = accepting = False
    try:
        failure = None  # (status, code, audited reason), raised outside the deadline
        try:
            async with asyncio.timeout(settings.files.approval_copy_deadline_seconds):
                spool = await _thread(stopped, open_spool)
                try:
                    intact = await _thread(stopped, read_source, storage, target, spool, stopped)
                except Stopped:
                    raise
                except Exception:
                    intact, failure = None, (503, "approval_storage_unavailable", "source_unavailable")
                if intact is False:
                    failure = (409, "review_file_integrity_failed", "checksum_mismatch")
                if failure is None:
                    # Grant/actor again before writing a new object; nothing is held over the put.
                    await audited(proof, principal, rid, body, csrf, clock, settings, target)
                    if stopped.is_set() or await _disconnected(request):
                        raise Stopped()
                    put_started = True  # even a failing put may leave an object at this key
                    try:
                        if not await _thread(stopped, store_copy, storage, key, target, spool, stopped):
                            failure = (503, "approval_storage_unavailable", "copy_mismatch")
                    except Stopped:
                        raise
                    except Exception:
                        failure = (503, "approval_storage_unavailable", "copy_failed")
        except TimeoutError:
            failure = (503, "approval_copy_timeout", "copy_timeout")
        if failure is not None:
            await _thread(stopped, refusal_audit, principal, rid, csrf, clock, settings, AuditOutcome.FAILURE,
                          failure[2])
            raise ApiError(failure[0], failure[1])
        if stopped.is_set() or await _disconnected(request):
            raise Stopped()
        accepting = True
        approval = await audited(accept, principal, target, body, rights, csrf, clock, settings, key,
                                 stopped, outcome)
        return await _thread(stopped, respond, principal, approval, csrf, clock, settings)
    except Stopped:
        raise ApiError(503, "approval_unavailable") from None
    except ApiError:
        raise
    except Exception:
        # Never a 500 with details; a known commit stays saved, without a DTO.
        raise ApiError(503, "approval_response_unavailable" if outcome.committed else
                       "approval_outcome_uncertain" if outcome.commit_started else "approval_unavailable") from None
    finally:
        # Every thread has settled here (_thread waits even when cancelled), so
        # the outcome is final. Delete only a key no commit can reference.
        try:
            if put_started and (not accepting or (outcome.rollback_confirmed and not outcome.commit_started)):
                await _thread(stopped, discard, storage, key)
        finally:
            try:
                if spool is not None:
                    await _thread(stopped, close_spool, spool)
            finally:
                slots.release()


async def receive(request, principal, raw_id, body, clock, settings, storage):
    stopped = threading.Event()
    task = asyncio.create_task(attempt(request, principal, raw_id, body,
        request.headers.get("x-csrf-token") or "", clock, settings, storage, stopped))
    _attempts.add(task)

    def settled(done):
        _attempts.discard(done)
        if not done.cancelled():
            done.exception()  # consume detached failures without logging payloads

    task.add_done_callback(settled)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The attempt keeps slot/spool/key/outcome until its threads settle;
        # no DTO, no retry. A started commit is allowed to settle.
        stopped.set()
        raise
