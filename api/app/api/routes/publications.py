"""Admin views and actions for common copies approved from a review (M6b-1, M6b-2a, M6b-2b2b).

A ``pending`` common item is absent from every normal route (library, count,
collections, files, search, chunks, RAG), for the Admin too; so is a
``revoked`` one. A ``listed`` (published) copy shows its *metadata* on the
normal library routes; its original opens on ``/files/{id}/original`` only
when its rights permit download without conditions (M6b-2b1,
``original_download``). ``content_status`` stays ``metadata_only`` and
chunk/search/RAG still refuse it.

- ``/admin/pending-publications``: read-only view of pending copies (M6b-1).
- ``/admin/common-copies``: every reviewed copy by state, with the recorded
  rights, publication record, index status and tombstone/cleanup status;
  ``publish`` (pending -> listed), ``index`` (start or return the index job of
  a listed copy whose rights permit full-text search and external processing,
  M6b-2b2b) and ``revoke`` (pending or listed -> revoked). Revoke may name, per
  metadata field, a specific basis to keep it after cleanup; every field
  without one is removed at cleanup.

No route here returns file bytes, object keys or extracted text, or calls a
provider: ``index`` only queues a durable job for the worker. An indexed copy
is still served by no chunk, search or RAG route.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_now, require_admin, require_trusted_origin
from app.api.errors import ApiError
from app.authz import Principal, pending_publication_clause, review_copy_admin_clause
from app import common_index
from app.common_original import common_original_rows
from app.config import Settings, get_settings
from app.models import (AccessRights, CommonPublication, DeletionTombstone, LibraryItem, PaperVersion,
                        RevocationMetadataRetention, SourceFile, User)
from app.models.enums import PublicationState, RevocationReason, RevokedMetadataField
from app.publication import RetentionBasis, live_duplicate, publish, revoke
from app.storage import OriginalStorage, get_storage

router = APIRouter(prefix="/admin/pending-publications", tags=["review"])
copies_router = APIRouter(prefix="/admin/common-copies", tags=["review"])

PENDING_NOTE = ("Bản chung đã có quyền do Admin ghi nhận nhưng chưa công bố: User khác không thấy metadata, "
                "file, kết quả tìm kiếm, chunk hay RAG của bản này. Chưa có index; công bố là bước riêng.")
LISTED_NOTE = ("Đã công bố metadata: mọi tài khoản thấy tiêu đề và version trong thư viện. File gốc chỉ tải được khi "
               "quyền đã ghi cho phép tải và không có điều kiện sử dụng (hệ thống chưa thi hành được điều kiện dạng "
               "text); mỗi lần tải đều kiểm lại quyền và SHA-256. Toàn văn chưa khả dụng: chưa có tìm kiếm toàn văn, "
               "chunk hay hỏi đáp cho bản này, kể cả khi quyền đã ghi cho phép. Admin có thể lập chỉ mục khi quyền "
               "cho tìm kiếm toàn văn và gửi qua provider, không kèm điều kiện; đã lập chỉ mục vẫn chưa mở tìm "
               "kiếm/hỏi đáp.")
REVOKED_NOTE = ("Đã thu hồi: không route nào phục vụ bản này từ lúc thu hồi commit. Cleanup xóa object chung, dữ "
                "liệu phát sinh và metadata (tiêu đề, URL nguồn, tên file, nguồn/giấy phép/bằng chứng/điều kiện) trừ "
                "trường có căn cứ giữ riêng; bản riêng của User không đổi.")
NOTES = {PublicationState.PENDING.value: PENDING_NOTE, PublicationState.LISTED.value: LISTED_NOTE,
         PublicationState.REVOKED.value: REVOKED_NOTE}


class IndexInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The page says the copy's extracted text goes to OpenRouter for embedding; nothing is pre-ticked.
    confirm_external_processing: Literal[True]


class PublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_publish: Literal[True]
    # The checksum the Admin saw on the page: publishing names these exact bytes.
    file_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _basis_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("empty")
    return value


class RetentionInput(BaseModel):
    """A specific basis to keep one field of this copy after cleanup (never the value)."""

    model_config = ConfigDict(extra="forbid")
    field: RevokedMetadataField
    policy_source: Annotated[str, Field(max_length=500), AfterValidator(_basis_text)]
    scope: Annotated[str, Field(max_length=500), AfterValidator(_basis_text)]
    reason: Annotated[str, Field(max_length=1000), AfterValidator(_basis_text)]


class RevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_revoke: Literal[True]
    reason_code: RevocationReason
    # Default: nothing kept. Each entry is confirmed by the revoking Admin.
    retain: list[RetentionInput] = Field(default_factory=list, max_length=len(RevokedMetadataField))
    confirm_retention_basis: bool = False


def _storage(settings: Settings = Depends(get_settings)) -> OriginalStorage:
    return get_storage(settings)


def _parse_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError(404, "not_found") from None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _rows(clause):
    return (select(LibraryItem, PaperVersion, SourceFile, AccessRights, User.email)
            .join(AccessRights, AccessRights.item_id == LibraryItem.id)
            .join(PaperVersion, PaperVersion.id == AccessRights.version_id)
            .join(SourceFile, SourceFile.id == AccessRights.source_file_id)
            .join(User, User.id == AccessRights.reviewer_user_id)
            .where(clause))


def _gate_opens(db: Session, admin: Principal, source_file_id: uuid.UUID) -> bool:
    """The download route's own SQL gate, for this file, now (audience: every account)."""
    return db.execute(common_original_rows(admin).where(SourceFile.id == source_file_id)).first() is not None


def original_download_status(item: LibraryItem, rights: AccessRights, gate_opens: bool) -> str:
    """Why the copy's original does or does not download, for the Admin page.

    ``open`` only when ``common_original_clause`` itself opens the file
    (``gate_opens``); the other codes explain a closed gate. ``rights_not_current``
    covers what the recorded rights alone cannot show: an epoch that moved after
    the rights were recorded, a publication that is not live, or a file, version,
    checksum or size that no longer matches. Each download still re-checks all of
    it and the object's SHA-256.
    """
    if gate_opens:
        return "open"
    if item.publication_state == PublicationState.PENDING.value:
        return "not_published"
    if item.publication_state == PublicationState.REVOKED.value or rights.revoked_at is not None:
        return "revoked"
    if not rights.permits_original_download:
        return "not_permitted"
    if rights.scope_conditions is not None:
        return "blocked_by_conditions"
    return "rights_not_current"


def _out(item: LibraryItem, version: PaperVersion, source: SourceFile, rights: AccessRights, reviewer: str,
         gate_opens: bool = False) -> dict:
    return {
        "id": str(item.id), "title": item.title, "visibility": item.visibility,
        "publication_state": item.publication_state,
        "published": item.publication_state == PublicationState.LISTED.value,
        # Publication is metadata only; full text is a separate gate (set from the index status in _full_out).
        "content_status": item.content_status, "fulltext_available": False,
        "original_download": original_download_status(item, rights, gate_opens),
        "created_at": item.created_at.isoformat(), "note": NOTES[item.publication_state],
        "version": {"id": str(version.id), "source_url": version.source_url,
                    "received_at": version.received_at.isoformat()},
        "file": {"id": str(source.id), "filename": source.original_filename, "verified_mime": source.verified_mime,
                 "byte_size": source.byte_size, "page_count": source.page_count,
                 "checksum_sha256": source.checksum_sha256},
        "rights": {
            "id": str(rights.id), "origin_kind": rights.origin_kind,
            "origin_review_request_id": str(rights.origin_review_request_id),
            "file_checksum_sha256": rights.file_checksum_sha256,
            "verified_source": rights.verified_source, "verified_source_url": rights.verified_source_url,
            "license_url": rights.license_url, "evidence_note": rights.evidence_note,
            "checked_on": rights.checked_on.isoformat(), "recorded_at": rights.recorded_at.isoformat(),
            "reviewer_email": reviewer,
            "scope": {"audience": rights.scope_audience, "original_download": rights.permits_original_download,
                      "fulltext_search": rights.permits_fulltext_search,
                      "external_processing": rights.permits_external_processing,
                      "conditions": rights.scope_conditions},
            "revoked_at": _iso(rights.revoked_at), "revocation_reason_code": rights.revocation_reason_code,
            "metadata_removed_at": _iso(rights.metadata_scrubbed_at),
        },
    }


def _full_out(db: Session, admin: Principal, row: tuple, now: datetime) -> dict:
    item, source = row[0], row[2]
    out = _out(*row, gate_opens=_gate_opens(db, admin, source.id))
    publication = db.get(CommonPublication, item.id)
    out["publication"] = None if publication is None else {
        "published_at": publication.published_at.isoformat(), "revoked_at": _iso(publication.revoked_at),
        "verified_byte_size": publication.verified_byte_size}
    if item.publication_state == PublicationState.PENDING.value:
        # Admin-only: a live copy with the same bytes blocks publishing this one.
        duplicate = live_duplicate(db, out["file"]["checksum_sha256"], item.id)
        out["duplicate_published_item_id"] = str(duplicate) if duplicate else None
    out["index"] = common_index.status(db, item, get_settings())
    out["fulltext_available"] = out["index"]["search_open"]
    tombstone = db.scalar(select(DeletionTombstone).where(DeletionTombstone.item_id == item.id))
    out["tombstone"] = None if tombstone is None else {
        "id": str(tombstone.id), "reason_code": tombstone.reason_code,
        "prior_publication_state": tombstone.prior_publication_state, "recorded_at": tombstone.recorded_at.isoformat(),
        "purge_due_at": tombstone.purge_due_at.isoformat(), "purged_at": _iso(tombstone.purged_at),
        "object_outcome": tombstone.object_outcome, "cleanup_attempts": tombstone.cleanup_attempts,
        "last_cleanup_error": tombstone.last_cleanup_error,
        "overdue": tombstone.purged_at is None and now > tombstone.purge_due_at,
        "removed_fields": tombstone.scrubbed_fields, "retained_fields": tombstone.retained_fields}
    bases = db.execute(select(RevocationMetadataRetention, User.email)
                       .join(User, User.id == RevocationMetadataRetention.confirmed_by_user_id)
                       .where(RevocationMetadataRetention.item_id == item.id)
                       .order_by(RevocationMetadataRetention.field)).all()
    out["retention_bases"] = [{"field": b.field, "policy_source": b.policy_source, "scope": b.scope,
                               "reason": b.reason, "confirmed_by_email": email,
                               "recorded_at": b.recorded_at.isoformat()} for b, email in bases]
    return out


@router.get("")
def list_pending(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    stmt = _rows(pending_publication_clause(admin))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(LibraryItem.created_at.desc(), LibraryItem.id).limit(limit).offset(offset)).all()
    return {"items": [_out(*row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{item_id}")
def get_pending(item_id: str, admin: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    row = db.execute(_rows(pending_publication_clause(admin)).where(LibraryItem.id == _parse_id(item_id))).first()
    if row is None:
        raise ApiError(404, "not_found")
    return {"item": _out(*row)}


@copies_router.get("")
def list_copies(
    state: Literal["pending", "listed", "revoked"] = "pending",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict:
    stmt = _rows(review_copy_admin_clause(admin)).where(LibraryItem.publication_state == state)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(LibraryItem.updated_at.desc(), LibraryItem.id).limit(limit).offset(offset)).all()
    return {"items": [_full_out(db, admin, row, now) for row in rows], "total": total, "state": state,
            "limit": limit, "offset": offset}


def _copy_out(db: Session, admin: Principal, item_id: uuid.UUID, now: datetime) -> dict:
    row = db.execute(_rows(review_copy_admin_clause(admin)).where(LibraryItem.id == item_id)).first()
    if row is None:
        raise ApiError(404, "not_found")
    return _full_out(db, admin, row, now)


@copies_router.get("/{item_id}")
def get_copy(item_id: str, admin: Principal = Depends(require_admin), db: Session = Depends(get_db),
             now: datetime = Depends(get_now)) -> dict:
    return {"item": _copy_out(db, admin, _parse_id(item_id), now)}


@copies_router.post("/{item_id}/publish", dependencies=[Depends(require_trusted_origin)])
def publish_copy(
    item_id: str,
    body: PublishInput,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    storage: OriginalStorage = Depends(_storage),
) -> dict:
    done = publish(db, admin, storage, _parse_id(item_id), body.file_checksum_sha256, now)
    return {"item": _copy_out(db, admin, done.item.id, now)}


@copies_router.post("/{item_id}/index", dependencies=[Depends(require_trusted_origin)])
def index_copy(
    item_id: str,
    body: IndexInput,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict:
    """Queue (or return the running) index job; the worker does the rest under the same gate."""
    started = common_index.start(db, admin, _parse_id(item_id), now)
    return {"item": _copy_out(db, admin, started.job.item_id, now),
            "job": {"id": str(started.job.id), "state": started.job.state}, "created": started.created}


@copies_router.post("/{item_id}/revoke", dependencies=[Depends(require_trusted_origin)])
def revoke_copy(
    item_id: str,
    body: RevokeInput,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    fields = [r.field for r in body.retain]
    if len(set(fields)) != len(fields):
        raise ApiError(422, "retention_field_repeated")
    if body.retain and not body.confirm_retention_basis:
        raise ApiError(422, "retention_confirmation_required")
    retain = tuple(RetentionBasis(r.field, r.policy_source, r.scope, r.reason) for r in body.retain)
    done = revoke(db, admin, _parse_id(item_id), body.reason_code, now, settings.retention.deleted_cleanup_deadline,
                  retain, journal_path=settings.recovery.journal_path)
    return {"item": _copy_out(db, admin, done.item.id, now), "cancelled_jobs": done.cancelled_jobs}
