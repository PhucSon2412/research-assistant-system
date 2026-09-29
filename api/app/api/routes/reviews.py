"""Owner review requests for one exact private version/file (M6a).

The owner submits, lists and withdraws their own requests. While a request is
open, an Admin may read the metadata needed for review and stream the one
original it names; nothing else of the item (other versions/files, chunks,
search, RAG, collections) opens, and the normal routes are unchanged. Closing
(withdraw/reject) commits before any later review read and is final. This
module creates no common item, AccessRights row, index or publication.

Audit rows carry IDs and reason codes only: never declared rights text, the
rejection note, titles, file bytes or tokens.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_now, get_principal, require_admin, require_trusted_origin
from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, m2a_private_derivatives_clause, review_grant_clause
from app.config import Settings, get_settings
from app.models import LibraryItem, PaperVersion, ReviewRequest, SourceFile, User
from app.models.enums import AuditActorType, AuditOutcome, ReviewRejectReason, ReviewStatus
from app.models.review import (DECLARED_TEXT_MAX_CHARS, DECLARED_URL_MAX_CHARS, REJECTION_NOTE_MAX_CHARS,
                               WITHDRAWN_BY_REQUESTER)
from app.storage import OriginalStorage, get_storage

router = APIRouter(prefix="/review-requests", tags=["review"])
admin_router = APIRouter(prefix="/admin/review-requests", tags=["review"])

NOTICE = (
    "Khi gửi yêu cầu duyệt, Admin được cấp quyền xem tạm metadata cần thiết và file gốc của đúng "
    "version/file bạn chọn để kiểm tra nguồn và quyền phân phối. Quyền này chỉ tồn tại khi yêu cầu còn mở "
    "và kết thúc với mọi lần mở mới ngay khi bạn rút yêu cầu hoặc Admin từ chối; phần file đã truyền trước "
    "lúc đó không thể thu hồi. Admin không xem được version/file khác, chunk, kết quả tìm kiếm hay hỏi đáp "
    "trên kho riêng của bạn. Yêu cầu đang chờ duyệt chưa phải là công bố: paper vẫn chỉ ở kho riêng. "
    "Thông tin nguồn/quyền bạn khai báo là lời khai của bạn, chưa được Admin xác minh."
)
DECLARATION_NOTE = "Lời khai của User; chưa được Admin xác minh, không phải bằng chứng quyền phân phối."
PENDING_NOTE = "Yêu cầu đang chờ duyệt chưa phải là công bố; paper vẫn chỉ ở kho riêng của User."
STREAM_CHUNK_BYTES = 64 * 1024


def _clean_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("empty")
    return value


def _clean_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")) or any(c.isspace() for c in value):
        raise ValueError("url must be http(s) without spaces")
    return value


class ReviewRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged: Literal[True]
    notice_version: str = Field(min_length=1, max_length=100)
    item_id: uuid.UUID
    version_id: uuid.UUID
    source_file_id: uuid.UUID
    declared_source: str = Field(min_length=1, max_length=DECLARED_TEXT_MAX_CHARS)
    declared_source_url: str | None = Field(default=None, max_length=DECLARED_URL_MAX_CHARS)
    declared_rights: str = Field(min_length=1, max_length=DECLARED_TEXT_MAX_CHARS)
    declared_license_url: str | None = Field(default=None, max_length=DECLARED_URL_MAX_CHARS)

    @field_validator("declared_source", "declared_rights")
    @classmethod
    def _texts(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator("declared_source_url", "declared_license_url")
    @classmethod
    def _urls(cls, value: str | None) -> str | None:
        return _clean_url(value)


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: ReviewRejectReason
    note: str | None = Field(default=None, max_length=REJECTION_NOTE_MAX_CHARS)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _storage(settings: Settings = Depends(get_settings)) -> OriginalStorage:
    return get_storage(settings)


def _parse_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError(404, "not_found") from None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _closure_out(rr: ReviewRequest) -> dict:
    return {"id": str(rr.id), "status": rr.status, "submitted_at": rr.submitted_at.isoformat(),
            "closed_at": _iso(rr.closed_at), "close_reason_code": rr.close_reason_code}


def _declaration_out(rr: ReviewRequest) -> dict:
    return {"source": rr.declared_source, "source_url": rr.declared_source_url,
            "rights": rr.declared_rights, "license_url": rr.declared_license_url,
            "verified": False, "note": DECLARATION_NOTE}


def _target_out(rr: ReviewRequest, item: LibraryItem, version: PaperVersion, source: SourceFile) -> dict:
    return {"item": {"id": str(item.id), "title": item.title},
            "version": {"id": str(version.id), "version_label": version.version_label,
                        "source_identifier": version.source_identifier, "source_url": version.source_url,
                        "received_at": version.received_at.isoformat()},
            "file": {"id": str(source.id), "filename": source.original_filename,
                     "verified_mime": source.verified_mime, "byte_size": source.byte_size,
                     "page_count": source.page_count, "checksum_sha256": rr.file_checksum_sha256}}


def _owner_out(rr: ReviewRequest, item: LibraryItem, version: PaperVersion, source: SourceFile) -> dict:
    return _closure_out(rr) | _target_out(rr, item, version, source) | {
        "declaration": _declaration_out(rr), "rejection_note": rr.rejection_note,
        "notice_version": rr.notice_version, "publication": False, "pending_note": PENDING_NOTE}


def _audit(db: Session, principal: Principal, action: str, outcome: AuditOutcome, rr_id: uuid.UUID | str,
           **details: str) -> None:
    record_event(db, action=action, outcome=outcome, actor_type=AuditActorType.USER,
                 actor_user_id=principal.user_id, target_type="review_request", target_id=str(rr_id),
                 details=details)


def _owned_rows(principal: Principal):
    # The requester is the item owner by FK; the private gate re-applies ownership.
    return (select(ReviewRequest, LibraryItem, PaperVersion, SourceFile)
            .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
            .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
            .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
            .where(ReviewRequest.requester_user_id == principal.user_id, m2a_private_derivatives_clause(principal)))


def _owned_request(db: Session, principal: Principal, request_id: str, *, lock: bool = False):
    stmt = _owned_rows(principal).where(ReviewRequest.id == _parse_id(request_id))
    row = db.execute(stmt.with_for_update(of=ReviewRequest) if lock else stmt).first()
    if row is None:
        raise ApiError(404, "not_found")
    return row


@router.get("/notice")
def notice(principal: Principal = Depends(get_principal), settings: Settings = Depends(get_settings)) -> dict:
    return {"version": settings.review.notice_version, "text": NOTICE}


@router.get("")
def list_own(
    item_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    stmt = _owned_rows(principal)
    if item_id is not None:
        stmt = stmt.where(ReviewRequest.item_id == _parse_id(item_id))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(ReviewRequest.submitted_at.desc(), ReviewRequest.id)
                      .limit(limit).offset(offset)).all()
    return {"requests": [_owner_out(*row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201, dependencies=[Depends(require_trusted_origin)])
def submit(
    body: ReviewRequestInput,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    if principal.is_admin:
        # Admins do not upload private papers and must not review their own.
        raise ApiError(403, "forbidden")
    if body.notice_version != settings.review.notice_version:
        raise ApiError(409, "notice_changed")
    # One gate for "not mine", "missing" and "file not in that version of that item".
    target = db.execute(
        select(LibraryItem, PaperVersion, SourceFile)
        .join(PaperVersion, PaperVersion.item_id == LibraryItem.id)
        .join(SourceFile, SourceFile.version_id == PaperVersion.id)
        .where(LibraryItem.id == body.item_id, PaperVersion.id == body.version_id,
               SourceFile.id == body.source_file_id, m2a_private_derivatives_clause(principal))
        .with_for_update(of=LibraryItem, read=True)
    ).first()
    if target is None:
        db.rollback()
        raise ApiError(404, "not_found")
    item, version, source = target
    rr = ReviewRequest(item_id=item.id, version_id=version.id, source_file_id=source.id,
                       requester_user_id=principal.user_id, status=ReviewStatus.OPEN.value,
                       file_checksum_sha256=source.checksum_sha256, rights_epoch_at_submit=item.rights_epoch,
                       notice_version=body.notice_version, declared_source=body.declared_source,
                       declared_source_url=body.declared_source_url, declared_rights=body.declared_rights,
                       declared_license_url=body.declared_license_url, submitted_at=now)
    db.add(rr)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == "uq_review_requests_open_file":
            raise ApiError(409, "review_request_open") from None
        raise
    _audit(db, principal, "review.request.submit", AuditOutcome.SUCCESS, rr.id,
           item_id=str(item.id), version_id=str(version.id), source_file_id=str(source.id))
    db.commit()
    return {"request": _owner_out(rr, item, version, source)}


@router.get("/{request_id}")
def get_own(request_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return {"request": _owner_out(*_owned_request(db, principal, request_id))}


@router.post("/{request_id}/withdraw", dependencies=[Depends(require_trusted_origin)])
def withdraw(
    request_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict:
    rr, item, version, source = _owned_request(db, principal, request_id, lock=True)
    if rr.status != ReviewStatus.OPEN.value:
        db.rollback()
        raise ApiError(409, "review_request_closed")
    rr.status = ReviewStatus.WITHDRAWN.value
    rr.closed_at = now
    rr.closed_by_user_id = principal.user_id
    rr.close_reason_code = WITHDRAWN_BY_REQUESTER
    _audit(db, principal, "review.request.withdraw", AuditOutcome.SUCCESS, rr.id, reason_code=WITHDRAWN_BY_REQUESTER)
    # From this commit every new Admin review read of the file is refused.
    db.commit()
    return {"request": _owner_out(rr, item, version, source)}


# --- Admin -----------------------------------------------------------------

def _granted(principal: Principal):
    return (select(ReviewRequest, LibraryItem, PaperVersion, SourceFile, User.email)
            .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
            .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
            .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
            .join(User, User.id == ReviewRequest.requester_user_id)
            .where(review_grant_clause(principal)))


def _admin_out(rr: ReviewRequest, granted: tuple | None) -> dict:
    out = _closure_out(rr) | {"review_available": granted is not None, "publication": False,
                              "pending_note": PENDING_NOTE}
    if granted is None:
        # Closed (or no longer granted): only what the Admin already decided on.
        return out
    _, item, version, source, email = granted
    return out | _target_out(rr, item, version, source) | {
        "requester_email": email, "declaration": _declaration_out(rr),
        "original_url": f"/admin/review-requests/{rr.id}/files/{source.id}/original"}


@admin_router.get("")
def admin_list(
    status: Literal["open", "closed"] = "open",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    where = (ReviewRequest.status == ReviewStatus.OPEN.value if status == "open"
             else ReviewRequest.status != ReviewStatus.OPEN.value)
    total = db.scalar(select(func.count()).select_from(ReviewRequest).where(where))
    rows = db.scalars(select(ReviewRequest).where(where)
                      .order_by(ReviewRequest.submitted_at.desc(), ReviewRequest.id).limit(limit).offset(offset)).all()
    granted = {g[0].id: g for g in db.execute(_granted(admin).where(ReviewRequest.id.in_([r.id for r in rows])))} \
        if rows else {}
    return {"requests": [_admin_out(r, granted.get(r.id)) for r in rows], "total": total,
            "limit": limit, "offset": offset, "status": status}


@admin_router.get("/{request_id}")
def admin_get(request_id: str, admin: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    parsed = _parse_id(request_id)
    rr = db.get(ReviewRequest, parsed)
    if rr is None:
        raise ApiError(404, "not_found")
    return {"request": _admin_out(rr, db.execute(_granted(admin).where(ReviewRequest.id == parsed)).first())}


def _deny_reason(rr: ReviewRequest | None, file_id: uuid.UUID) -> str | None:
    if rr is None:
        return None
    if rr.status != ReviewStatus.OPEN.value:
        return "request_closed"
    if rr.source_file_id != file_id:
        return "file_not_in_request"
    return "grant_inactive"


@admin_router.get("/{request_id}/files/{file_id}/original")
def admin_original(
    request_id: str,
    file_id: str,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    storage: OriginalStorage = Depends(_storage),
) -> StreamingResponse:
    rid, fid = _parse_id(request_id), _parse_id(file_id)
    granted = _granted(admin).where(ReviewRequest.id == rid, SourceFile.id == fid)

    def deny(reason_code: str | None) -> ApiError:
        db.rollback()
        if reason_code is not None:
            _audit(db, admin, "review.original.view", AuditOutcome.DENIED, rid, reason_code=reason_code)
            db.commit()
        return ApiError(404, "not_found")

    row = db.execute(granted).first()
    if row is None:
        raise deny(_deny_reason(db.get(ReviewRequest, rid), fid))
    rr, _item, _version, source, _email = row
    db.rollback()  # no lock held while reading the object
    spool = tempfile.TemporaryFile()
    try:
        # Verify the exact bytes the owner submitted before sending any of them.
        try:
            body = storage.open(source.storage_key)
        except Exception:
            raise ApiError(503, "file_unavailable") from None
        digest, size = hashlib.sha256(), 0
        try:
            for chunk in body.iter_chunks(chunk_size=STREAM_CHUNK_BYTES):
                size += len(chunk)
                if size > source.byte_size:
                    break
                digest.update(chunk)
                spool.write(chunk)
        except Exception:
            raise ApiError(503, "file_unavailable") from None
        finally:
            body.close()
        if size != source.byte_size or digest.hexdigest() != rr.file_checksum_sha256:
            _audit(db, admin, "review.original.view", AuditOutcome.FAILURE, rid, reason_code="checksum_mismatch")
            db.commit()
            raise ApiError(409, "review_file_integrity_failed")
        # Re-check the grant under a share lock: a withdraw/reject committed
        # while verifying wins; one arriving later waits for this audit row.
        if db.execute(granted.with_for_update(of=ReviewRequest, read=True)).first() is None:
            raise deny(_deny_reason(db.get(ReviewRequest, rid), fid))
        _audit(db, admin, "review.original.view", AuditOutcome.SUCCESS, rid, source_file_id=str(source.id))
        db.commit()
    except BaseException:
        spool.close()
        raise
    spool.seek(0)

    def chunks():
        # Bytes sent after a later close commits cannot be recalled (docs/access-and-data.md).
        try:
            while block := spool.read(STREAM_CHUNK_BYTES):
                yield block
        finally:
            spool.close()

    filename = quote(source.original_filename or "original", safe="")
    return StreamingResponse(chunks(), media_type=source.verified_mime, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Content-Length": str(source.byte_size),
        "Cache-Control": "no-store",
    })


@admin_router.post("/{request_id}/reject", dependencies=[Depends(require_trusted_origin)])
def admin_reject(
    request_id: str,
    body: RejectInput,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict:
    rr = db.scalar(select(ReviewRequest).where(ReviewRequest.id == _parse_id(request_id)).with_for_update())
    if rr is None:
        db.rollback()
        raise ApiError(404, "not_found")
    if rr.status != ReviewStatus.OPEN.value:
        db.rollback()
        raise ApiError(409, "review_request_closed")
    rr.status = ReviewStatus.REJECTED.value
    rr.closed_at = now
    rr.closed_by_user_id = admin.user_id
    rr.close_reason_code = body.reason_code.value
    rr.rejection_note = body.note
    # The note stays on the request for the owner; audit keeps only the code.
    _audit(db, admin, "review.request.reject", AuditOutcome.SUCCESS, rr.id, reason_code=body.reason_code.value)
    db.commit()
    return {"request": _admin_out(rr, None)}
