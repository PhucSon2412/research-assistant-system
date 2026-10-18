"""Minimal library metadata API (M1b): list, count, detail, edit title.

Permission filtering happens in SQL before ordering/pagination/counting.
Direct access by ID re-applies the same filter, and a forbidden private item
answers exactly like a missing one (404 ``not_found``). M1c includes
ACL-filtered file/job metadata in item detail; file bytes have a separate route.
A common item lists a file (with its download path) only when that file's
live AccessRights open it for download (M6b-2b1, ``common_original_clause``).
Its chunks, coverage, references and metadata candidates open only for the
active version/file its live rights permit for full-text search (M6b-2b3a,
``common_derivatives_clause``); the two gates are independent. ``rag_available``
of a common copy is the RAG gate (M6b-2b3b): searchable text whose rights also
permit external processing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, StrictBool, ConfigDict, field_validator
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_db, get_principal, require_trusted_origin
from app.api.errors import ApiError
from app.api.resource_reads import resource_json
from app.api.resource_writes import Mutation, resource_write
from app.api.cookies import CSRF_HEADER
from app.config import Settings, get_settings
from app.audit import record_event
from app.authz import (Principal, can_edit_item, common_derivatives_clause, common_fulltext_items_clause,
                       common_original_clause, m2a_private_derivatives_clause, visible_items_clause)
from app.citations import RELATION_NOTE, citation_link_out, hidden_discovery_clause
from app.models import (Chunk, DiscoveryPaper, IngestionJob, LibraryItem, MetadataCandidate, PaperVersion, PdfPage,
                        ReferenceCitationLink, ReferenceEntry, Section, SourceFile)
from app.ingest import duplicates
from app.models.enums import AuditActorType, AuditOutcome, ContentStatus, JobState, Visibility

router = APIRouter(prefix="/library", tags=["library"])


class DeleteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_delete: StrictBool = Field(json_schema_extra={'const': True})

    @field_validator('confirm_delete')
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError('explicit confirmation required')
        return value


class DeleteReceipt(BaseModel):
    item_id: uuid.UUID
    deleted: Literal[True]
    purge_due_at: datetime
    cleanup_status: Literal['pending','completed']


@router.post('/items/{item_id}/delete', dependencies=[Depends(require_trusted_origin)], response_model=DeleteReceipt)
async def delete_item(request: Request, item_id: str, body: DeleteInput,
    principal: Principal = Depends(get_principal), clock=Depends(get_clock), settings: Settings=Depends(get_settings)) -> dict:
    """Permanently remove the owner's entire private item; physical cleanup is asynchronous."""
    from app.api import private_delete
    return await private_delete.receive(request,principal,item_id,clock,settings)


class Scope(StrEnum):
    """Narrows what the caller may already see; it can never widen it."""

    ALL = "all"
    COMMON = "common"
    MINE = "mine"


class ItemPatch(BaseModel):
    title: str | None = Field(default=None, max_length=1000)


def _scope_clause(principal: Principal, scope: Scope) -> ColumnElement[bool]:
    clause = visible_items_clause(principal)
    if scope is Scope.COMMON:
        return and_(clause, LibraryItem.visibility == Visibility.COMMON.value)
    if scope is Scope.MINE:
        return and_(clause, LibraryItem.owner_user_id == principal.user_id)
    return clause


def _searchable_common(db: Session, principal: Principal, items, *,
                       external_processing: bool = False) -> set[uuid.UUID]:
    """Common items among ``items`` whose indexed text search serves now (same gate as the search route).

    ``external_processing=True``: RAG's gate, whose evidence also goes to a provider.
    """
    ids = [i.id for i in items if i.visibility == Visibility.COMMON.value]
    if not ids:
        return set()
    return set(db.scalars(select(LibraryItem.id).where(LibraryItem.id.in_(ids), common_fulltext_items_clause(
        principal, external_processing=external_processing))).all())


def _item_out(item: LibraryItem, principal: Principal, searchable_common: set[uuid.UUID] = frozenset(),
              ragable_common: set[uuid.UUID] = frozenset()) -> dict:
    return {
        "id": str(item.id),
        "visibility": item.visibility,
        # Owner identity is never exposed; only whether it is the caller.
        "owned_by_me": item.owner_user_id == principal.user_id,
        "content_status": item.content_status,
        # Whether full-text search serves this item's text: an owner's indexed private item, or a common
        # copy under its live full-text rights (M6b-2b3a). ``indexed`` alone is not opened.
        "fulltext_search_available": (item.id in searchable_common if item.visibility == Visibility.COMMON.value
                                      else item.content_status == ContentStatus.INDEXED.value),
        # RAG uses a common copy only while its rights permit search and external processing (M6b-2b3b);
        # this flag shows the form, the server still applies the gate (and pricing) on every request.
        "rag_available": (item.id in ragable_common if item.visibility == Visibility.COMMON.value
                          else item.content_status == ContentStatus.INDEXED.value),
        "title": item.title,
        "active_version_id": str(item.active_version_id) if item.active_version_id else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "can_edit": can_edit_item(principal, item),
    }


def _get_visible_item(db: Session, principal: Principal, item_id: str, *, for_update: bool = False) -> LibraryItem:
    try:
        parsed = uuid.UUID(item_id)
    except ValueError:
        raise ApiError(404, "not_found") from None
    stmt = select(LibraryItem).where(LibraryItem.id == parsed, visible_items_clause(principal))
    if for_update:
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if item is None:
        raise ApiError(404, "not_found")
    return item


def _get_derivative_item(db: Session, principal: Principal, item_id: str) -> LibraryItem:
    """The caller's private item, or a common copy whose text may be served now; otherwise 404."""
    try:
        parsed = uuid.UUID(item_id)
    except ValueError:
        raise ApiError(404, "not_found") from None
    item = db.scalar(select(LibraryItem).where(
        LibraryItem.id == parsed, or_(m2a_private_derivatives_clause(principal),
                                      common_fulltext_items_clause(principal))))
    if item is None:
        raise ApiError(404, "not_found")
    return item


def _derived_rows(principal: Principal, model) -> ColumnElement[bool]:
    """Rows of ``model`` the caller may read: any of their own private item, or only the active version/file
    of a servable common copy. The outer query must join ``LibraryItem`` on ``model.item_id``."""
    return or_(m2a_private_derivatives_clause(principal),
               common_derivatives_clause(principal, model.item_id, model.version_id, model.source_file_id))


@router.get("/items")
def list_items(
    scope: Scope = Scope.ALL,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_list_items(scope=scope, limit=limit, offset=offset, principal=current, db=db),
        item_ids=lambda data: [row["id"] for row in data["items"]])


def _read_list_items(
    scope: Scope,
    limit: int,
    offset: int,
    principal: Principal,
    db: Session,
) -> dict:
    where = _scope_clause(principal, scope)
    total = db.scalar(select(func.count()).select_from(LibraryItem).where(where))
    items = db.scalars(
        select(LibraryItem)
        .where(where)
        .order_by(LibraryItem.created_at.desc(), LibraryItem.id)
        .limit(limit)
        .offset(offset)
    ).all()
    searchable = _searchable_common(db, principal, items)
    ragable = _searchable_common(db, principal, items, external_processing=True)
    return {
        "items": [_item_out(i, principal, searchable, ragable) for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
        "scope": scope.value,
    }


@router.get("/items/count")
def count_items(
    scope: Scope = Scope.ALL, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_count_items(scope=scope, principal=current, db=db))


def _read_count_items(
    scope: Scope, principal: Principal, db: Session
) -> dict:
    total = db.scalar(select(func.count()).select_from(LibraryItem).where(_scope_clause(principal, scope)))
    return {"total": total, "scope": scope.value}


@router.get("/items/{item_id}")
def get_item(item_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    # A duplicate's navigation target is SHARE-locked and re-read like the item itself.
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_get_item(item_id=item_id, principal=current, db=db),
        item_ids=lambda data: [data["item"]["id"]] + [j["existing_copy"]["item_id"]
            for j in data["item"]["jobs"] if j["existing_copy"]])


def _read_get_item(item_id: str, principal: Principal, db: Session) -> dict:
    item = _get_visible_item(db, principal, item_id)
    versions = db.scalars(
        select(PaperVersion).where(PaperVersion.item_id == item.id).order_by(PaperVersion.received_at)
    ).all()
    # A common item lists only the file its live rights open for download
    # (M6b-2b1), from the same SQL gate as the download route; otherwise none.
    file_gate = PaperVersion.item_id == item.id
    if item.visibility == Visibility.COMMON.value:
        file_gate = and_(file_gate, common_original_clause(principal))
    files = db.scalars(
        select(SourceFile).join(PaperVersion, SourceFile.version_id == PaperVersion.id)
        .join(LibraryItem, PaperVersion.item_id == LibraryItem.id)
        .where(file_gate).order_by(SourceFile.created_at, SourceFile.id)
    ).all()
    jobs = db.scalars(select(IngestionJob).where(IngestionJob.item_id == item.id)).all() \
        if item.visibility == Visibility.PRIVATE.value else []
    searchable = _searchable_common(db, principal, [item])
    ragable = _searchable_common(db, principal, [item], external_processing=True)
    return {
        "item": _item_out(item, principal, searchable, ragable)
        | {
            "versions": [
                {
                    "id": str(v.id),
                    "version_label": v.version_label,
                    "source_identifier": v.source_identifier,
                    "source_url": v.source_url,
                    # A common copy's extraction coverage shows with its searchable (active) version only.
                    "coverage": v.coverage if (item.visibility == Visibility.PRIVATE.value or (
                        item.id in searchable and v.id == item.active_version_id)) else None,
                    "received_at": v.received_at.isoformat(),
                }
                for v in versions
            ],
            "files": [{"id": str(f.id), "version_id": str(f.version_id),
                         "filename": f.original_filename, "byte_size": f.byte_size,
                         "verified_mime": f.verified_mime, "page_count": f.page_count,
                         "original_url": f"/files/{f.id}/original"}
                        for f in files],
            "jobs": [{"id": str(j.id), "state": j.state, "origin": j.origin,
                      "attempt_count": j.attempt_count, "last_error_code": j.last_error_code,
                      "warnings": j.warnings if item.visibility == Visibility.PRIVATE.value else [],
                      "existing_copy": _existing_copy(db, principal, item, j),
                      "queue_wait_ms": int((j.started_at - j.created_at).total_seconds() * 1000)
                          if j.started_at else None,
                      "extraction_finished_at": j.extraction_finished_at.isoformat()
                          if j.extraction_finished_at else None,
                      "extraction_processing_ms": int((j.extraction_finished_at - j.started_at).total_seconds() * 1000)
                          if j.extraction_finished_at and j.started_at else None,
                      "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                      "processing_ms": int((j.finished_at - j.started_at).total_seconds() * 1000)
                          if j.finished_at and j.started_at else None}
                     for j in jobs],
        }
    }


def _existing_copy(db: Session, principal: Principal, item: LibraryItem, job: IngestionJob) -> dict | None:
    """M6e: the owner's current live copy of a duplicate's exact bytes, for navigation only.

    Recomputed on every read with the worker's eligibility predicate and the
    caller's private ACL; never stored, never canonical, never another owner's
    or a common copy. ``None`` when no target is currently valid.
    """
    if (job.state != JobState.DUPLICATE.value or duplicates.WARNING not in (job.warnings or [])
            or item.visibility != Visibility.PRIVATE.value or item.owner_user_id != principal.user_id):
        return None
    source = db.scalar(select(SourceFile).where(SourceFile.job_id == job.id, SourceFile.item_id == item.id))
    if source is None:
        return None
    found = db.execute(duplicates.targets(principal.user_id, source.checksum_sha256,
                                          exclude_file_id=source.id).limit(1)).first()
    if found is None:
        return None
    target = db.execute(select(LibraryItem.id, LibraryItem.title, LibraryItem.content_status)
        .where(LibraryItem.id == found.item_id, LibraryItem.id != item.id, visible_items_clause(principal),
               LibraryItem.visibility == Visibility.PRIVATE.value)).first()
    if target is None:
        return None
    return {"item_id": str(target.id), "title": target.title, "content_status": target.content_status}


@router.get("/items/{item_id}/chunks")
def get_chunks(
    item_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_get_chunks(item_id=item_id, limit=limit, offset=offset, principal=current, db=db),
        item_ids=lambda data: [item_id])


def _read_get_chunks(
    item_id: str,
    limit: int,
    offset: int,
    principal: Principal,
    db: Session,
) -> dict:
    item = _get_derivative_item(db, principal, item_id)
    rows = db.execute(select(Chunk, Section.kind).join(Section, Chunk.section_id == Section.id)
        .join(LibraryItem, Chunk.item_id == LibraryItem.id)
        .where(Chunk.item_id == item.id, _derived_rows(principal, Chunk))
        .order_by(Chunk.version_id, Chunk.source_file_id, Chunk.start_page, Chunk.start_offset, Chunk.id)
        .limit(limit).offset(offset)).all()
    return {"chunks": [{"id": str(c.id), "version_id": str(c.version_id),
            "source_file_id": str(c.source_file_id), "text": c.text,
            "section_kind": kind,
            "locator_kind": c.locator_kind,
            "start_offset": c.start_offset, "end_offset": c.end_offset,
            "start_line": c.start_line, "end_line": c.end_line,
            "start_page": c.start_page, "end_page": c.end_page,
            "locator_detail": c.locator_detail}
            for c, kind in rows], "limit": limit, "offset": offset}


@router.get("/items/{item_id}/metadata-candidates")
def get_metadata_candidates(
    item_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_get_metadata_candidates(item_id=item_id, principal=current, db=db),
        item_ids=lambda data: [item_id])


def _read_get_metadata_candidates(
    item_id: str,
    principal: Principal,
    db: Session,
) -> dict:
    item = _get_derivative_item(db, principal, item_id)
    rows = db.scalars(select(MetadataCandidate).join(LibraryItem, MetadataCandidate.item_id == LibraryItem.id)
        .where(MetadataCandidate.item_id == item.id, _derived_rows(principal, MetadataCandidate))).all()
    return {"candidates": [{"field_name": r.field_name, "value": r.value,
            "version_id": str(r.version_id), "source_file_id": str(r.source_file_id),
            "locator_kind": r.locator_kind, "start_offset": r.start_offset,
            "end_offset": r.end_offset, "start_page": r.start_page}
            for r in rows]}


@router.get("/items/{item_id}/coverage")
def get_coverage(item_id: str, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_get_coverage(item_id=item_id, principal=current, db=db),
        item_ids=lambda data: [item_id])


def _read_get_coverage(item_id: str, principal: Principal,
                 db: Session) -> dict:
    item = _get_derivative_item(db, principal, item_id)
    rows = db.scalars(select(PdfPage).join(LibraryItem, PdfPage.item_id == LibraryItem.id)
        .where(PdfPage.item_id == item.id, _derived_rows(principal, PdfPage))
        .order_by(PdfPage.version_id, PdfPage.source_file_id, PdfPage.page_number)).all()
    summaries = {}
    for p in rows:
        summary = summaries.setdefault(str(p.source_file_id), {"source_file_id": str(p.source_file_id),
            "version_id": str(p.version_id), "body_pages": 0, "qualifying_pages": 0,
            "deficient_pages": 0, "unextractable_pages": [], "total_body_chars": 0})
        if p.body_page:
            summary["body_pages"] += 1
            summary["qualifying_pages" if p.qualifies else "deficient_pages"] += 1
        summary["total_body_chars"] += p.body_chars
        if p.error_code:
            summary["unextractable_pages"].append(p.page_number)
    return {"summary": list(summaries.values()), "pages": [{"version_id": str(p.version_id), "source_file_id": str(p.source_file_id),
        "page": p.page_number, "role": p.role, "body_chars": p.body_chars,
        "body_page": p.body_page, "qualifies": p.qualifies, "error_code": p.error_code,
        "layout_mode": p.layout_mode,
        "page_width_pt": p.width, "page_height_pt": p.height, "parser": p.pipeline_version}
        for p in rows]}


@router.get("/items/{item_id}/references")
def get_references(item_id: str, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    return resource_json(db, principal, clock=clock, idle=settings.auth.session_idle,
        read=lambda current: _read_get_references(item_id=item_id, principal=current, db=db),
        item_ids=lambda data: [item_id], citations=True)


def _read_get_references(item_id: str, principal: Principal,
                   db: Session) -> dict:
    item = _get_derivative_item(db, principal, item_id)
    rows = db.execute(select(ReferenceEntry, ReferenceCitationLink, DiscoveryPaper,
                             hidden_discovery_clause().label("target_hidden"))
        .join(LibraryItem, ReferenceEntry.item_id == LibraryItem.id)
        .outerjoin(ReferenceCitationLink, ReferenceCitationLink.reference_id == ReferenceEntry.id)
        .outerjoin(DiscoveryPaper, DiscoveryPaper.id == ReferenceCitationLink.discovery_paper_id)
        .where(ReferenceEntry.item_id == item.id, _derived_rows(principal, ReferenceEntry))
        .order_by(ReferenceEntry.version_id, ReferenceEntry.source_file_id, ReferenceEntry.ordinal)).all()
    return {"references": [{"id": str(r.id), "ordinal": r.ordinal,
        "version_id": str(r.version_id), "source_file_id": str(r.source_file_id),
        "raw_citation": r.raw_citation, "fields": r.fields, "confidence": r.confidence,
        "provenance": r.provenance, "pipeline_version": r.pipeline_version, "locator_kind": r.locator_kind,
        "page": r.page_number, "start_offset": r.start_offset, "end_offset": r.end_offset,
        "evidence_kind": "bibliography_entry",
        "citation_link": citation_link_out(r, link, paper, hidden)} for r, link, paper, hidden in rows],
        "citation_relation_note": RELATION_NOTE}


@router.patch("/items/{item_id}")
def update_item(
    item_id: str,
    body: ItemPatch,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    clock=Depends(get_clock), settings: Settings = Depends(get_settings),
) -> dict:
    def check(current):
        item = _get_visible_item(db, current, item_id)
        if not can_edit_item(current, item):
            raise ApiError(403, "forbidden")

    def read(current):
        item = _get_visible_item(db, current, item_id)
        return {"item": _item_out(item, current, _searchable_common(db, current, [item]),
            _searchable_common(db, current, [item], external_processing=True))}

    def mutate(current):
        item = _get_visible_item(db, current, item_id, for_update=True)
        if not can_edit_item(current, item):
            raise ApiError(403, "forbidden")
        if "title" in body.model_fields_set:
            item.title = body.title
        item.updated_at = clock()
        record_event(db, action="library.item.update", outcome=AuditOutcome.SUCCESS,
            actor_type=AuditActorType.USER, actor_user_id=current.user_id,
            target_type="library_item", target_id=str(item.id),
            details={"fields": sorted(body.model_fields_set)})
        return Mutation(check, read, item_ids=lambda data: [data["item"]["id"]])

    return resource_write(db, principal, csrf_token=request.headers.get(CSRF_HEADER) or "",
        clock=clock, idle=settings.auth.session_idle, mutate=mutate)
