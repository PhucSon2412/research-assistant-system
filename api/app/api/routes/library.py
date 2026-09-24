"""Minimal library metadata API (M1b): list, count, detail, edit title.

Permission filtering happens in SQL before ordering/pagination/counting.
Direct access by ID re-applies the same filter, and a forbidden private item
answers exactly like a missing one (404 ``not_found``). M1c includes
ACL-filtered file/job metadata in item detail; file bytes have a separate route.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_now, get_principal
from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, can_edit_item, visible_items_clause
from app.models import IngestionJob, LibraryItem, PaperVersion, SourceFile
from app.models.enums import AuditActorType, AuditOutcome, Visibility

router = APIRouter(prefix="/library", tags=["library"])


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


def _item_out(item: LibraryItem, principal: Principal) -> dict:
    return {
        "id": str(item.id),
        "visibility": item.visibility,
        # Owner identity is never exposed; only whether it is the caller.
        "owned_by_me": item.owner_user_id == principal.user_id,
        "content_status": item.content_status,
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


@router.get("/items")
def list_items(
    scope: Scope = Scope.ALL,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
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
    return {
        "items": [_item_out(i, principal) for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
        "scope": scope.value,
    }


@router.get("/items/count")
def count_items(
    scope: Scope = Scope.ALL, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> dict:
    total = db.scalar(select(func.count()).select_from(LibraryItem).where(_scope_clause(principal, scope)))
    return {"total": total, "scope": scope.value}


@router.get("/items/{item_id}")
def get_item(item_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    item = _get_visible_item(db, principal, item_id)
    versions = db.scalars(
        select(PaperVersion).where(PaperVersion.item_id == item.id).order_by(PaperVersion.received_at)
    ).all()
    files = db.scalars(
        select(SourceFile).join(PaperVersion, SourceFile.version_id == PaperVersion.id)
        .where(PaperVersion.item_id == item.id)
    ).all()
    jobs = db.scalars(select(IngestionJob).where(IngestionJob.item_id == item.id)).all()
    return {
        "item": _item_out(item, principal)
        | {
            "versions": [
                {
                    "id": str(v.id),
                    "version_label": v.version_label,
                    "source_identifier": v.source_identifier,
                    "source_url": v.source_url,
                    "coverage": v.coverage,
                    "received_at": v.received_at.isoformat(),
                }
                for v in versions
            ],
            "files": [{"id": str(f.id), "version_id": str(f.version_id),
                         "filename": f.original_filename, "byte_size": f.byte_size,
                         "verified_mime": f.verified_mime, "page_count": f.page_count}
                        for f in files],
            "jobs": [{"id": str(j.id), "state": j.state, "origin": j.origin}
                     for j in jobs],
        }
    }


@router.patch("/items/{item_id}")
def update_item(
    item_id: str,
    body: ItemPatch,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
) -> dict:
    item = _get_visible_item(db, principal, item_id, for_update=True)
    if not can_edit_item(principal, item):
        # The item is visible (common) but not editable by a User.
        db.rollback()
        raise ApiError(403, "forbidden")
    changed = sorted(body.model_fields_set)
    if "title" in body.model_fields_set:
        item.title = body.title
    item.updated_at = now
    record_event(
        db,
        action="library.item.update",
        outcome=AuditOutcome.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user_id=principal.user_id,
        target_type="library_item",
        target_id=str(item.id),
        details={"fields": changed},
    )
    db.commit()
    return {"item": _item_out(item, principal)}
