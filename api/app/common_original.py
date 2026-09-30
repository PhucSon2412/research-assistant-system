"""Verified download of a published common copy's original (M6b-2b1).

``GET /files/{id}/original`` serves a *common* file only through
``common_original_clause``: a ``listed`` copy whose live publication and live
AccessRights name exactly this item/version/file/checksum at the item's
current ``rights_epoch``, with ``permits_original_download`` for
``instance_users`` and no (unenforceable) ``scope_conditions``. Every other
common file answers ``404`` like a missing ID. The private-owner branch of the
same route is unchanged and does not come through here.

Order, so that no byte leaves before the bytes and the rights are both proved:

1. The route's first gate query finds the file; the transaction then ends so
   no lock or snapshot is held while reading object storage.
2. Take one of ``RAS_FILES__VERIFIED_DOWNLOAD_MAX_CONCURRENT`` spool slots
   (none free -> ``503 download_busy``), read the object in chunks into an
   anonymous temp file, never more than the recorded size, and compare size
   and SHA-256 with the common ``SourceFile`` (equal, by the gate, to the
   rights' and the publication's). Missing or different bytes, same size
   included -> ``409 common_file_integrity_failed``; storage errors ->
   ``503 file_unavailable``. Nothing has been sent.
3. Lock the item row ``FOR SHARE``. Publish and revoke lock it ``FOR UPDATE``
   first (then rights, then publication), so this single lock never inverts
   their order: a revoke that committed while step 2 ran is seen by the next
   statement; one still running is waited for, then seen. Re-check the session
   (at the time *now*, not the request's start: an idle or absolute expiry
   reached while reading refuses) and re-run the whole gate as a new statement
   (READ COMMITTED: a fresh snapshot); the file, key, checksum, size and epoch
   must be the ones verified. Failure -> ``401``/``404``. Record the audit row
   and commit, which releases the lock.
4. Stream the verified temp file. The slot and the file are released on every
   path: a failure above, the end of the stream, a client that disconnects
   before or after the first chunk (``VerifiedFileResponse``), or, as a last
   resort, when the unclosed object is garbage-collected.

Limit: a revoke that commits after step 3 (``t_rev`` later than the last
check) does not stop a response that already passed it, and bytes already
sent cannot be recalled. Every request that starts its step 3 after ``t_rev``,
including a saved URL, is refused.

Audit rows carry IDs and codes only: no title, file name, URL, checksum or bytes.
"""

from __future__ import annotations

import tempfile
import threading
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import IO

from fastapi.responses import StreamingResponse

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, common_original_clause
from app.config import Settings
from app.identity.sessions import session_is_live
from app.models import LibraryItem, PaperVersion, SourceFile
from app.models.enums import AuditActorType, AuditOutcome
from app.promotion import verified_read
from app.storage import OriginalStorage, is_missing_object

STREAM_CHUNK_BYTES = 64 * 1024
ACTION = "common_copy.original.download"


def common_original_rows(principal: Principal) -> Select:
    return (select(SourceFile, LibraryItem.rights_epoch)
            .join(PaperVersion, SourceFile.version_id == PaperVersion.id)
            .join(LibraryItem, PaperVersion.item_id == LibraryItem.id)
            .where(common_original_clause(principal)))


@dataclass(frozen=True)
class _Target:
    file_id: uuid.UUID
    item_id: uuid.UUID
    storage_key: str
    byte_size: int
    checksum: str
    rights_epoch: int


def _target(source: SourceFile, rights_epoch: int) -> _Target:
    return _Target(source.id, source.item_id, source.storage_key, source.byte_size, source.checksum_sha256,
                   rights_epoch)


@lru_cache
def _slots(limit: int) -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(limit)


class _Hold:
    """One spool slot and the temp file it pays for; released exactly once."""

    def __init__(self, slots: threading.BoundedSemaphore):
        self.slots, self.spool, self.done = slots, None, False
        self.lock = threading.Lock()

    def release(self) -> None:
        with self.lock:
            if self.done:
                return
            self.done = True
        try:
            if self.spool is not None:
                self.spool.close()
        finally:
            self.slots.release()


class VerifiedOriginal:
    """Bytes already proved, in a temp file that holds one spool slot.

    An iterator object rather than a generator: ``close()`` frees the file and
    the slot even when nothing was read yet (the ``finally`` of a generator
    that never started does not run). ``VerifiedFileResponse`` calls it however
    the response ends; a finalizer frees them if the object is dropped unclosed.
    """

    def __init__(self, slots: threading.BoundedSemaphore):
        self._hold = _Hold(slots)
        self._finalizer = weakref.finalize(self, self._hold.release)
        self._started = False

    def open_spool(self) -> IO[bytes]:
        self._hold.spool = tempfile.TemporaryFile(prefix="ras-common-original-")
        return self._hold.spool

    @property
    def closed(self) -> bool:
        return self._hold.done

    def close(self) -> None:
        self._finalizer()  # runs _Hold.release at most once, then detaches

    def __iter__(self) -> "VerifiedOriginal":
        return self

    def __next__(self) -> bytes:
        # Bytes sent after a later revoke commits cannot be recalled (docs/access-and-data.md).
        block = b""
        with self._hold.lock:
            spool = None if self._hold.done else self._hold.spool
            if spool is not None:
                if not self._started:
                    spool.seek(0)
                    self._started = True
                block = spool.read(STREAM_CHUNK_BYTES)
        if not block:
            self.close()
            raise StopIteration
        return block


class VerifiedFileResponse(StreamingResponse):
    """Streams a ``VerifiedOriginal`` and closes it however the response ends:
    completion, client disconnect (before or after the first chunk) or error."""

    def __init__(self, verified: VerifiedOriginal, **kwargs):
        super().__init__(verified, **kwargs)
        self._verified = verified

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._verified.close()


def _audit(db: Session, principal: Principal, outcome: AuditOutcome, target: _Target, **details: str) -> None:
    record_event(db, action=ACTION, outcome=outcome, actor_type=AuditActorType.USER,
                 actor_user_id=principal.user_id, target_type="source_file", target_id=str(target.file_id),
                 details={"item_id": str(target.item_id), **details})


def _refuse(db: Session, principal: Principal, target: _Target, outcome: AuditOutcome, reason: str,
            error: ApiError) -> ApiError:
    db.rollback()
    try:
        _audit(db, principal, outcome, target, reason_code=reason)
        db.commit()
    except Exception:  # the refusal stands even if its audit row cannot be written
        db.rollback()
    return error


def prepare_common_original(db: Session, principal: Principal, storage: OriginalStorage, source: SourceFile,
                            rights_epoch: int, *, clock: Callable[[], datetime],
                            settings: Settings) -> VerifiedOriginal:
    """Steps 1-3 above for a file the route's gate query matched through ``common_original_clause``.

    ``clock`` is read again at the last check: a session that expired (idle or
    absolute) while the object was being read does not get the bytes.
    """
    target = _target(source, rights_epoch)
    db.rollback()  # no lock or snapshot while reading the object
    slots = _slots(settings.files.verified_download_max_concurrent)
    if not slots.acquire(blocking=False):
        raise ApiError(503, "download_busy")
    verified = VerifiedOriginal(slots)  # owns the slot from here: every exit below releases it
    try:
        try:
            spool = verified.open_spool()
        except OSError:
            raise ApiError(503, "file_unavailable") from None
        try:
            intact = verified_read(storage, target.storage_key, target.byte_size, target.checksum, spool)
        except Exception as exc:
            if is_missing_object(exc):
                raise _refuse(db, principal, target, AuditOutcome.FAILURE, "object_missing",
                              ApiError(409, "common_file_integrity_failed")) from None
            raise _refuse(db, principal, target, AuditOutcome.FAILURE, "storage_unavailable",
                          ApiError(503, "file_unavailable")) from None
        if not intact:
            raise _refuse(db, principal, target, AuditOutcome.FAILURE, "checksum_mismatch",
                          ApiError(409, "common_file_integrity_failed"))
        # Last check before the first byte, under the lock publish/revoke take first.
        db.execute(select(LibraryItem.id).where(LibraryItem.id == target.item_id).with_for_update(read=True))
        if not session_is_live(db, principal.session_id, now=clock(), idle=settings.auth.session_idle):
            db.rollback()
            raise ApiError(401, "session_expired", clear_session=True)
        again = db.execute(common_original_rows(principal).where(SourceFile.id == target.file_id)).first()
        if again is None or _target(*again) != target:
            raise _refuse(db, principal, target, AuditOutcome.DENIED, "rights_changed", ApiError(404, "not_found"))
        _audit(db, principal, AuditOutcome.SUCCESS, target)
        db.commit()
    except BaseException:
        db.rollback()
        verified.close()
        raise
    return verified
