"""Admin views and actions for common copies approved from a review (M6b-1, M6b-2a).

A ``pending`` common item is absent from every normal route (library, count,
collections, files, search, chunks, RAG), for the Admin too; so is a
``revoked`` one. A ``listed`` (published) copy shows its *metadata* on the
normal library routes and nothing else: ``content_status`` stays
``metadata_only`` and file/chunk/search/RAG still refuse it.

- ``/admin/pending-publications``: read-only view of pending copies (M6b-1).
- ``/admin/common-copies``: every reviewed copy by state, with the recorded
  rights, publication record and tombstone/cleanup status; ``publish``
  (pending -> listed) and ``revoke`` (pending or listed -> revoked). Revoke
  may name, per metadata field, a specific basis to keep it after cleanup;
  every field without one is removed at cleanup.

No route here returns file bytes or object keys, creates a job or index, or
calls a provider.
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
LISTED_NOTE = ("Đã công bố metadata: mọi tài khoản thấy tiêu đề và version trong thư viện. Toàn văn chưa khả dụng: "
               "chưa có file gốc, tìm kiếm toàn văn, chunk hay hỏi đáp cho bản này, kể cả khi quyền đã ghi cho phép.")
REVOKED_NOTE = ("Đã thu hồi: không route nào phục vụ bản này từ lúc thu hồi commit. Cleanup xóa object chung, dữ "
                "liệu phát sinh và metadata (tiêu đề, URL nguồn, tên file, nguồn/giấy phép/bằng chứng/điều kiện) trừ "
                "trường có căn cứ giữ riêng; bản riêng của User không đổi.")
NOTES = {PublicationState.PENDING.value: PENDING_NOTE, PublicationState.LISTED.value: LISTED_NOTE,
         PublicationState.REVOKED.value: REVOKED_NOTE}


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


def _out(item: LibraryItem, version: PaperVersion, source: SourceFile, rights: AccessRights, reviewer: str) -> dict:
    return {
        "id": str(item.id), "title": item.title, "visibility": item.visibility,
        "publication_state": item.publication_state,
        "published": item.publication_state == PublicationState.LISTED.value,
        # Publication is metadata only; full text is a separate, later gate.
        "content_status": item.content_status, "fulltext_available": False,
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


def _full_out(db: Session, row: tuple, now: datetime) -> dict:
    item = row[0]
    out = _out(*row)
    publication = db.get(CommonPublication, item.id)
    out["publication"] = None if publication is None else {
        "published_at": publication.published_at.isoformat(), "revoked_at": _iso(publication.revoked_at),
        "verified_byte_size": publication.verified_byte_size}
    if item.publication_state == PublicationState.PENDING.value:
        # Admin-only: a live copy with the same bytes blocks publishing this one.
        duplicate = live_duplicate(db, out["file"]["checksum_sha256"], item.id)
        out["duplicate_published_item_id"] = str(duplicate) if duplicate else None
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
    return {"items": [_full_out(db, row, now) for row in rows], "total": total, "state": state,
            "limit": limit, "offset": offset}


def _copy_out(db: Session, admin: Principal, item_id: uuid.UUID, now: datetime) -> dict:
    row = db.execute(_rows(review_copy_admin_clause(admin)).where(LibraryItem.id == item_id)).first()
    if row is None:
        raise ApiError(404, "not_found")
    return _full_out(db, row, now)


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
                  retain)
    return {"item": _copy_out(db, admin, done.item.id, now), "cancelled_jobs": done.cancelled_jobs}
