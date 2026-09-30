"""Approve one review request into a new, pending common copy (M6b-1).

Order, so that no step can leave a servable or half-recorded common copy:

1. Read the grant without locks and let the Admin's confirmed checksum match
   the request's.
2. Copy the private original to a *new* object key: stream it, check size and
   SHA-256 against the request, upload, then read the copy back and check it
   again. A failure deletes the new key (nothing references it yet).
3. One transaction: lock the request ``FOR UPDATE`` and the private item
   ``FOR SHARE``, re-check status, grant, rights epoch, file and checksum, then
   create the common item (``pending``), version, file and AccessRights and
   close the request as ``approved``. Deferred DB triggers refuse a commit
   that lacks any of these.

If that transaction fails, the new object is deleted only when no SourceFile
references it; when the database cannot tell (commit outcome unknown), the
object stays for ``app.cli.reconcile_originals``. An unreferenced object is
never reachable through the API, and a pending copy is not served at all.
This module creates no index, job or provider call.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, review_grant_clause
from app.models import AccessRights, LibraryItem, PaperVersion, ReviewRequest, SourceFile
from app.models.enums import (AuditActorType, AuditOutcome, PublicationState, ReviewStatus, RightsAudience,
                              RightsOrigin, Visibility)
from app.models.review import APPROVED_BY_ADMIN
from app.storage import OriginalStorage

COPY_CHUNK_BYTES = 64 * 1024
COMMON_FILE_SOURCE = "review_approval"


@dataclass(frozen=True)
class RecordedRights:
    """The Admin's own findings; nothing here is taken from the User's claim."""

    title: str
    verified_source: str
    verified_source_url: str | None
    license_url: str | None
    evidence_note: str | None
    checked_on: date
    audience: RightsAudience
    permits_original_download: bool
    permits_fulltext_search: bool
    permits_external_processing: bool
    scope_conditions: str | None


@dataclass(frozen=True)
class Approved:
    request: ReviewRequest
    item: LibraryItem
    version: PaperVersion
    source: SourceFile
    rights: AccessRights


def _audit(db: Session, admin: Principal, outcome: AuditOutcome, rid: uuid.UUID, **details: str) -> None:
    record_event(db, action="review.request.approve", outcome=outcome, actor_type=AuditActorType.USER,
                 actor_user_id=admin.user_id, target_type="review_request", target_id=str(rid), details=details)


def _refuse(db: Session, admin: Principal, rid: uuid.UUID, status: int, code: str, reason: str,
            outcome: AuditOutcome = AuditOutcome.DENIED) -> ApiError:
    db.rollback()
    try:
        _audit(db, admin, outcome, rid, reason_code=reason)
        db.commit()
    except Exception:  # the refusal stands even if its audit row cannot be written
        db.rollback()
    return ApiError(status, code)


def _granted(admin: Principal, rid: uuid.UUID):
    return (select(ReviewRequest, LibraryItem, PaperVersion, SourceFile)
            .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
            .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
            .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
            .where(ReviewRequest.id == rid, review_grant_clause(admin)))


def verified_read(storage: OriginalStorage, key: str, size: int, checksum: str, spool: IO[bytes] | None) -> bool:
    body = storage.open(key)
    digest, seen = hashlib.sha256(), 0
    try:
        for chunk in body.iter_chunks(chunk_size=COPY_CHUNK_BYTES):
            seen += len(chunk)
            if seen > size:
                return False
            digest.update(chunk)
            if spool is not None:
                spool.write(chunk)
    finally:
        body.close()
    return seen == size and digest.hexdigest() == checksum


def _discard(storage: OriginalStorage, key: str) -> None:
    try:
        storage.delete(key)
    except Exception:
        pass  # unreferenced, so unreachable; the orphan sweep removes it later


@dataclass(frozen=True)
class _Original:
    file_id: uuid.UUID
    storage_key: str
    byte_size: int
    mime: str
    checksum: str  # the request's, i.e. what the owner submitted


def _copy_original(db: Session, admin: Principal, storage: OriginalStorage, rid: uuid.UUID,
                   original: _Original) -> str:
    """Copy the reviewed bytes to a fresh key and prove the copy; return the key."""
    key = f"originals/{uuid.uuid4().hex}"
    with tempfile.TemporaryFile() as spool:
        try:
            intact = verified_read(storage, original.storage_key, original.byte_size, original.checksum, spool)
        except Exception:
            raise _refuse(db, admin, rid, 503, "approval_storage_unavailable", "source_unavailable",
                          AuditOutcome.FAILURE) from None
        if not intact:
            raise _refuse(db, admin, rid, 409, "review_file_integrity_failed", "checksum_mismatch",
                          AuditOutcome.FAILURE)
        try:
            storage.put(key, spool, original.mime)
            copied = verified_read(storage, key, original.byte_size, original.checksum, None)
        except Exception:
            _discard(storage, key)
            raise _refuse(db, admin, rid, 503, "approval_storage_unavailable", "copy_failed",
                          AuditOutcome.FAILURE) from None
    if not copied:
        _discard(storage, key)
        raise _refuse(db, admin, rid, 503, "approval_storage_unavailable", "copy_mismatch", AuditOutcome.FAILURE)
    return key


def _cleanup_after_failed_commit(db: Session, storage: OriginalStorage, key: str) -> bool:
    """Delete the copy only if provably unreferenced; True when nothing committed."""
    try:
        db.rollback()
        referenced = db.scalar(select(SourceFile.id).where(SourceFile.storage_key == key)) is not None
    except Exception:
        return False  # outcome unknown: keep the object for the orphan sweep
    if referenced:
        return False
    _discard(storage, key)
    return True


def approve(db: Session, admin: Principal, storage: OriginalStorage, rid: uuid.UUID, confirmed_checksum: str,
            rights: RecordedRights, now: datetime) -> Approved:
    row = db.execute(_granted(admin, rid)).first()
    if row is None:
        rr = db.get(ReviewRequest, rid)
        if rr is None:
            db.rollback()
            raise ApiError(404, "not_found")
        if rr.status != ReviewStatus.OPEN.value:
            raise _refuse(db, admin, rid, 409, "review_request_closed", "request_closed")
        raise _refuse(db, admin, rid, 409, "review_grant_inactive", "grant_inactive")
    rr, _item, _version, source = row
    if confirmed_checksum != rr.file_checksum_sha256:
        raise _refuse(db, admin, rid, 409, "checksum_confirmation_mismatch", "checksum_confirmation_mismatch")
    original = _Original(source.id, source.storage_key, source.byte_size, source.verified_mime,
                         rr.file_checksum_sha256)
    db.rollback()  # no lock or snapshot held while copying the object

    key = _copy_original(db, admin, storage, rid, original)
    try:
        locked = db.scalar(select(ReviewRequest).where(ReviewRequest.id == rid).with_for_update())
        if locked is None or locked.status != ReviewStatus.OPEN.value:
            _discard(storage, key)
            raise _refuse(db, admin, rid, 409, "review_request_closed", "request_closed")
        # Share-lock the private item so a concurrent epoch bump waits for us.
        row = db.execute(_granted(admin, rid).with_for_update(of=LibraryItem, read=True)).first()
        if (row is None or row[3].id != original.file_id or row[3].storage_key != original.storage_key
                or row[0].file_checksum_sha256 != original.checksum):
            _discard(storage, key)
            raise _refuse(db, admin, rid, 409, "review_grant_inactive", "grant_inactive")
        rr, _private_item, _private_version, source = row

        item = LibraryItem(visibility=Visibility.COMMON.value, owner_user_id=None,
                           publication_state=PublicationState.PENDING.value, title=rights.title,
                           rights_epoch=0, created_by_user_id=admin.user_id, created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        # Only the Admin-checked source URL describes the common version.
        version = PaperVersion(item_id=item.id, source_url=rights.verified_source_url, received_at=now,
                               created_at=now)
        db.add(version)
        db.flush()
        common_file = SourceFile(item_id=item.id, version_id=version.id, storage_key=key,
                                 verified_mime=source.verified_mime, byte_size=source.byte_size,
                                 page_count=source.page_count, checksum_sha256=rr.file_checksum_sha256,
                                 original_filename=source.original_filename, file_source=COMMON_FILE_SOURCE,
                                 created_at=now)
        db.add(common_file)
        db.flush()
        recorded = AccessRights(
            item_id=item.id, version_id=version.id, source_file_id=common_file.id,
            file_checksum_sha256=rr.file_checksum_sha256, origin_kind=RightsOrigin.REVIEW_REQUEST.value,
            origin_review_request_id=rr.id, verified_source=rights.verified_source,
            verified_source_url=rights.verified_source_url, license_url=rights.license_url,
            evidence_note=rights.evidence_note, checked_on=rights.checked_on, reviewer_user_id=admin.user_id,
            recorded_at=now, scope_audience=rights.audience.value,
            permits_original_download=rights.permits_original_download,
            permits_fulltext_search=rights.permits_fulltext_search,
            permits_external_processing=rights.permits_external_processing,
            scope_conditions=rights.scope_conditions, rights_epoch_at_grant=item.rights_epoch)
        db.add(recorded)
        rr.status = ReviewStatus.APPROVED.value
        rr.closed_at = now
        rr.closed_by_user_id = admin.user_id
        rr.close_reason_code = APPROVED_BY_ADMIN
        db.flush()
        _audit(db, admin, AuditOutcome.SUCCESS, rid, common_item_id=str(item.id),
               source_file_id=str(common_file.id), access_rights_id=str(recorded.id))
        # From this commit the review grant is over (the request is no longer open).
        db.commit()
    except ApiError:
        raise
    except Exception:
        if _cleanup_after_failed_commit(db, storage, key):
            raise ApiError(503, "approval_unavailable") from None
        raise ApiError(503, "approval_outcome_uncertain") from None
    return Approved(rr, item, version, common_file, recorded)
