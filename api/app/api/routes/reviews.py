"""Owner review requests for one exact private version/file (M6a).

The owner submits, lists and withdraws their own requests. While a request is
open, an Admin may read the metadata needed for review and stream the one
original it names; nothing else of the item (other versions/files, chunks,
search, RAG, collections) opens, and the normal routes are unchanged. Closing
(withdraw/reject/approve) commits before any later review read and is final.

Approval (M6b-1, ``app.promotion``) records the Admin's own rights findings and
creates a separate *pending* common copy; it publishes nothing, builds no
index and calls no provider.

Every close also appends a ``review_closure`` record (IDs, decision, code,
time) to the revocation journal before its commit (M6c-2b1,
``app.review_closing``), so restoring an older backup cannot reopen the grant.

Audit rows carry IDs and reason codes only: never declared rights text, the
rejection note, recorded evidence, titles, file bytes or tokens.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_db, get_now, get_principal, require_admin, require_trusted_origin
from app.api.errors import ApiError
from app.api.review_publication_reads import review_publication_json
from app.api import original_downloads, review_writes
from app.audit import record_event
from app.authz import (Principal, m2a_private_derivatives_clause, review_grant_clause,
                      review_history_admin_clause)
from app.config import Settings, get_settings
from app.models import AccessRights, LibraryItem, PaperVersion, ReviewRequest, SourceFile, User
from app.models.enums import AuditActorType, AuditOutcome, ReviewRejectReason, ReviewStatus, RightsAudience
from app.models.review import (DECLARED_TEXT_MAX_CHARS, DECLARED_URL_MAX_CHARS, REJECTION_NOTE_MAX_CHARS,
                               WITHDRAWN_BY_REQUESTER)
from app.models.rights import RIGHTS_TEXT_MAX_CHARS, RIGHTS_URL_MAX_CHARS, SCOPE_CONDITIONS_MAX_CHARS
from app.promotion import RecordedRights, approve
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
APPROVED_NOTE = ("Admin đã tự kiểm và ghi nhận quyền cho một bản chung riêng, đang chờ công bố; User khác chưa "
                 "thấy bản này. Bản riêng, version và collection của User không đổi.")
RECOVERED_NOTE = ("Yêu cầu đã được đóng khi khôi phục dữ liệu từ nhật ký: quyết định gốc (mã lý do) được giữ, "
                  "quyền xem tạm của Admin đã kết thúc. Nếu quyết định gốc là phê duyệt, bản chung tạo lúc đó không "
                  "có trong dữ liệu khôi phục và không được tạo lại; User có thể gửi yêu cầu mới.")
RIGHTS_NOTE = ("Quyền do Admin tự kiểm với đúng file/checksum này. Lời khai của User, việc User upload hay việc "
               "paper đọc miễn phí trên arXiv/Hugging Face không tự cấp quyền phân phối.")


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

    @field_validator("acknowledged", mode="before")
    @classmethod
    def _acknowledgement(cls, value):
        if value is not True:
            raise ValueError("explicit acknowledgement required")
        return value

    @field_validator("declared_source", "declared_rights")
    @classmethod
    def _texts(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator("declared_source_url", "declared_license_url")
    @classmethod
    def _urls(cls, value: str | None) -> str | None:
        return _clean_url(value)


def _optional_text(value: str | None) -> str | None:
    return (value.strip() or None) if value is not None else None


class ScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: RightsAudience
    original_download: bool
    fulltext_search: bool
    external_processing: bool
    conditions: str | None = Field(default=None, max_length=SCOPE_CONDITIONS_MAX_CHARS)

    @field_validator("conditions")
    @classmethod
    def _conditions(cls, value: str | None) -> str | None:
        return _optional_text(value)


class ApproveInput(BaseModel):
    """What the Admin checked themselves; the form starts empty, never from ``declared_*``."""

    model_config = ConfigDict(extra="forbid")
    confirm_verified: Literal[True]
    # The Admin confirms the exact bytes they checked.
    file_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=1000)
    verified_source: str = Field(min_length=1, max_length=RIGHTS_TEXT_MAX_CHARS)
    verified_source_url: str | None = Field(default=None, max_length=RIGHTS_URL_MAX_CHARS)
    license_url: str | None = Field(default=None, max_length=RIGHTS_URL_MAX_CHARS)
    evidence_note: str | None = Field(default=None, max_length=RIGHTS_TEXT_MAX_CHARS)
    checked_on: date
    scope: ScopeInput

    @field_validator("title", "verified_source")
    @classmethod
    def _texts(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator("evidence_note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("verified_source_url", "license_url")
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


def _pending_note(rr: ReviewRequest) -> str:
    if rr.status == ReviewStatus.CLOSED_BY_RECOVERY.value:
        return RECOVERED_NOTE
    if rr.status in (ReviewStatus.WITHDRAWN.value, ReviewStatus.REJECTED.value):
        return "Yêu cầu đã đóng; quyền xem tạm của Admin đã kết thúc. Paper vẫn ở kho riêng."
    return APPROVED_NOTE if rr.status == ReviewStatus.APPROVED.value else PENDING_NOTE


def _owner_out(rr: ReviewRequest, item: LibraryItem, version: PaperVersion, source: SourceFile) -> dict:
    # The owner learns the decision, never the common copy's ID or the recorded evidence.
    return _closure_out(rr) | _target_out(rr, item, version, source) | {
        "declaration": _declaration_out(rr), "rejection_note": rr.rejection_note,
        "notice_version": rr.notice_version, "publication": False, "pending_note": _pending_note(rr)}


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


# Read-only wrappers: never attach the boundary to shared POST formatters.
def _read_notice(principal: Principal = Depends(get_principal), settings: Settings = Depends(get_settings)) -> dict:
    return {"version": settings.review.notice_version, "text": NOTICE}


def _read_list_own(
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


def _read_get_own(request_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return {"request": _owner_out(*_owned_request(db, principal, request_id))}


def _read_admin_list(
    status: Literal["open", "closed"] = "open",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    where = (ReviewRequest.status == ReviewStatus.OPEN.value if status == "open"
             else ReviewRequest.status != ReviewStatus.OPEN.value)
    total = db.scalar(select(func.count()).select_from(ReviewRequest).where(where, review_history_admin_clause(admin)))
    rows = db.scalars(select(ReviewRequest).where(where, review_history_admin_clause(admin))
                      .order_by(ReviewRequest.submitted_at.desc(), ReviewRequest.id).limit(limit).offset(offset)).all()
    granted = {g[0].id: g for g in db.execute(_granted(admin).where(ReviewRequest.id.in_([r.id for r in rows])))} \
        if rows else {}
    return {"requests": [_admin_out(r, granted.get(r.id)) for r in rows], "total": total,
            "limit": limit, "offset": offset, "status": status}


def _read_admin_get(request_id: str, admin: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    parsed = _parse_id(request_id)
    rr = db.scalar(select(ReviewRequest).where(ReviewRequest.id == parsed, review_history_admin_clause(admin)))
    if rr is None:
        raise ApiError(404, "not_found")
    return {"request": _admin_out(rr, db.execute(_granted(admin).where(ReviewRequest.id == parsed)).first(),
                                  _pending_item_id(db, rr))}


@router.get("/notice")
def notice(principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings), clock=Depends(get_clock)) -> dict:
    return review_publication_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        review=True, read=lambda p: _read_notice(p, settings))


@router.get("")
def list_own(item_id: str | None = None, limit: int = Query(default=20, ge=1, le=100),
             offset: int = Query(default=0, ge=0, le=100_000), principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
             clock=Depends(get_clock)) -> dict:
    return review_publication_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        review=True, read=lambda p: _read_list_own(item_id, limit, offset, p, db))


@router.get("/{request_id}")
def get_own(request_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings), clock=Depends(get_clock)) -> dict:
    return review_publication_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        review=True, read=lambda p: _read_get_own(request_id, p, db))


@admin_router.get("")
def admin_list(status: Literal["open", "closed"] = "open", limit: int = Query(default=20, ge=1, le=100),
               offset: int = Query(default=0, ge=0, le=100_000), admin: Principal = Depends(require_admin),
               db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
               clock=Depends(get_clock)) -> dict:
    return review_publication_json(db, admin, clock=clock, idle=settings.auth.session_idle,
        review=True, admin=True, read=lambda p: _read_admin_list(status, limit, offset, p, db))


@admin_router.get("/{request_id}")
def admin_get(request_id: str, admin: Principal = Depends(require_admin), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings), clock=Depends(get_clock)) -> dict:
    return review_publication_json(db, admin, clock=clock, idle=settings.auth.session_idle,
        review=True, admin=True, read=lambda p: _read_admin_get(request_id, p, db))


@router.post("", status_code=201, dependencies=[Depends(require_trusted_origin)])
def submit(body: ReviewRequestInput, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
           clock=Depends(get_clock)) -> dict:
    return review_writes.write(db, principal, action="submit", body=body,
        csrf_token=request.headers.get("x-csrf-token", ""), clock=clock, settings=settings)


@router.post("/{request_id}/withdraw", dependencies=[Depends(require_trusted_origin)])
def withdraw(request_id: str, request: Request, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
             clock=Depends(get_clock)) -> dict:
    return review_writes.write(db, principal, action="withdraw", request_id=request_id,
        csrf_token=request.headers.get("x-csrf-token", ""), clock=clock, settings=settings)


# --- Admin -----------------------------------------------------------------

def _granted(principal: Principal):
    return (select(ReviewRequest, LibraryItem, PaperVersion, SourceFile, User.email)
            .join(SourceFile, SourceFile.id == ReviewRequest.source_file_id)
            .join(PaperVersion, PaperVersion.id == ReviewRequest.version_id)
            .join(LibraryItem, LibraryItem.id == ReviewRequest.item_id)
            .join(User, User.id == ReviewRequest.requester_user_id)
            .where(review_grant_clause(principal)))


def _admin_out(rr: ReviewRequest, granted: tuple | None, pending_item_id: uuid.UUID | None = None) -> dict:
    out = _closure_out(rr) | {"review_available": granted is not None, "publication": False,
                              "pending_note": _pending_note(rr)}
    if pending_item_id is not None:
        out["pending_publication_id"] = str(pending_item_id)
    if granted is None:
        # Closed (or no longer granted): only what the Admin already decided on.
        return out
    _, item, version, source, email = granted
    return out | _target_out(rr, item, version, source) | {
        "requester_email": email, "declaration": _declaration_out(rr), "rights_note": RIGHTS_NOTE,
        "original_url": f"/admin/review-requests/{rr.id}/files/{source.id}/original"}


def _pending_item_id(db: Session, rr: ReviewRequest) -> uuid.UUID | None:
    if rr.status != ReviewStatus.APPROVED.value:
        return None
    return db.scalar(select(AccessRights.item_id).where(AccessRights.origin_review_request_id == rr.id))


@admin_router.get("/{request_id}/files/{file_id}/original")
async def admin_original(
    request_id: str,
    file_id: str,
    request: Request,
    admin: Principal = Depends(require_admin),
    storage: OriginalStorage = Depends(_storage),
    clock=Depends(get_clock),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    return await original_downloads.receive(request, admin, file_id, clock, settings, storage, request_id)


@admin_router.post("/{request_id}/reject", dependencies=[Depends(require_trusted_origin)])
def admin_reject(request_id: str, body: RejectInput, request: Request,
                 admin: Principal = Depends(require_admin), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings), clock=Depends(get_clock)) -> dict:
    return review_writes.write(db, admin, action="reject", body=body, request_id=request_id,
        csrf_token=request.headers.get("x-csrf-token", ""), clock=clock, settings=settings)


@admin_router.post("/{request_id}/approve", dependencies=[Depends(require_trusted_origin)])
def admin_approve(
    request_id: str,
    body: ApproveInput,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    storage: OriginalStorage = Depends(_storage),
) -> dict:
    rid = _parse_id(request_id)
    if body.license_url is None and body.evidence_note is None:
        raise ApiError(422, "rights_evidence_required")
    if body.scope.external_processing and not body.scope.fulltext_search:
        raise ApiError(422, "rights_scope_inconsistent")
    # One day of slack for an Admin whose local date is ahead of UTC, as in the DB CHECK.
    if body.checked_on > now.date() + timedelta(days=1):
        raise ApiError(422, "checked_on_in_future")
    rights = RecordedRights(
        title=body.title, verified_source=body.verified_source, verified_source_url=body.verified_source_url,
        license_url=body.license_url, evidence_note=body.evidence_note, checked_on=body.checked_on,
        audience=body.scope.audience, permits_original_download=body.scope.original_download,
        permits_fulltext_search=body.scope.fulltext_search,
        permits_external_processing=body.scope.external_processing, scope_conditions=body.scope.conditions)
    done = approve(db, admin, storage, rid, body.file_checksum_sha256, rights, now)
    return {"request": _admin_out(done.request, None, done.item.id)}
