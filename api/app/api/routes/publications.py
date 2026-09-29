"""Admin view of common copies approved from a review but not published (M6b-1).

A ``pending`` common item is absent from every normal route (library, count,
collections, files, search, chunks, RAG), for the Admin too. This router is
the only way to see one, and only its metadata and the AccessRights the Admin
recorded: no file bytes, chunks or index, and nothing here can publish it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.errors import ApiError
from app.authz import Principal, pending_publication_clause
from app.models import AccessRights, LibraryItem, PaperVersion, SourceFile, User

router = APIRouter(prefix="/admin/pending-publications", tags=["review"])

PENDING_NOTE = ("Bản chung đã có quyền do Admin ghi nhận nhưng chưa công bố: User khác không thấy metadata, "
                "file, kết quả tìm kiếm, chunk hay RAG của bản này. Chưa có index; công bố là bước riêng.")


def _rows(admin: Principal):
    return (select(LibraryItem, PaperVersion, SourceFile, AccessRights, User.email)
            .join(AccessRights, AccessRights.item_id == LibraryItem.id)
            .join(PaperVersion, PaperVersion.id == AccessRights.version_id)
            .join(SourceFile, SourceFile.id == AccessRights.source_file_id)
            .join(User, User.id == AccessRights.reviewer_user_id)
            .where(pending_publication_clause(admin)))


def _out(item: LibraryItem, version: PaperVersion, source: SourceFile, rights: AccessRights, reviewer: str) -> dict:
    return {
        "id": str(item.id), "title": item.title, "visibility": item.visibility,
        "publication_state": item.publication_state, "published": False, "content_status": item.content_status,
        "created_at": item.created_at.isoformat(), "note": PENDING_NOTE,
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
        },
    }


@router.get("")
def list_pending(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    stmt = _rows(admin)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(LibraryItem.created_at.desc(), LibraryItem.id).limit(limit).offset(offset)).all()
    return {"items": [_out(*row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{item_id}")
def get_pending(item_id: str, admin: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    try:
        parsed = uuid.UUID(item_id)
    except ValueError:
        raise ApiError(404, "not_found") from None
    row = db.execute(_rows(admin).where(LibraryItem.id == parsed)).first()
    if row is None:
        raise ApiError(404, "not_found")
    return {"item": _out(*row)}
