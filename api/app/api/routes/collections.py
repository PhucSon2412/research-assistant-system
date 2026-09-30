"""Owner-only collection metadata. Membership never authorizes content reads."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal
from app.api.errors import ApiError
from app.authz import Principal, common_fulltext_items_clause, visible_items_clause
from app.models import Collection, CollectionItem, LibraryItem
from app.models.enums import ContentStatus, Visibility

router = APIRouter(prefix="/collections", tags=["collections"])


class CollectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("empty collection name")
        return value


class MembershipInput(BaseModel):
    item_id: uuid.UUID


def _owned(db: Session, principal: Principal, collection_id: uuid.UUID, *, lock: bool = False) -> Collection:
    stmt = select(Collection).where(Collection.id == collection_id, Collection.owner_user_id == principal.user_id)
    collection = db.scalar(stmt.with_for_update() if lock else stmt)
    if collection is None:
        raise ApiError(404, "not_found")
    return collection


def _out(collection: Collection) -> dict:
    return {"id": str(collection.id), "name": collection.name,
            "created_at": collection.created_at.isoformat(), "updated_at": collection.updated_at.isoformat()}


@router.get("")
def list_collections(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Collection).where(Collection.owner_user_id == principal.user_id)
                      .order_by(Collection.created_at, Collection.id)).all()
    return {"collections": [_out(row) for row in rows]}


@router.post("", status_code=201)
def create_collection(body: CollectionInput, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)) -> dict:
    row = Collection(owner_user_id=principal.user_id, name=body.name)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"collection": _out(row)}


@router.patch("/{collection_id}")
def rename_collection(collection_id: uuid.UUID, body: CollectionInput,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    row = _owned(db, principal, collection_id, lock=True)
    row.name = body.name
    row.updated_at = func.now()
    db.commit()
    db.refresh(row)
    return {"collection": _out(row)}


@router.delete("/{collection_id}", status_code=204)
def delete_collection(collection_id: uuid.UUID, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)) -> None:
    row = _owned(db, principal, collection_id, lock=True)
    try:
        db.execute(delete(CollectionItem).where(CollectionItem.collection_id == row.id))
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise


@router.get("/{collection_id}/items")
def list_collection_items(collection_id: uuid.UUID, limit: int = Query(20, ge=1, le=100),
                          offset: int = Query(0, ge=0, le=100_000),
                          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    collection = _owned(db, principal, collection_id)
    clause = (CollectionItem.collection_id == collection.id, visible_items_clause(principal))
    total = db.scalar(select(func.count()).select_from(CollectionItem)
                      .join(LibraryItem, LibraryItem.id == CollectionItem.item_id).where(*clause))
    rows = db.execute(select(LibraryItem, CollectionItem.added_at).join(CollectionItem, CollectionItem.item_id == LibraryItem.id)
                      .where(*clause).order_by(CollectionItem.added_at, LibraryItem.id)
                      .limit(limit).offset(offset)).all()
    # Whether search (and RAG) serve the item's text now: membership never decides it (same gates as
    # /search and /rag/ask).
    common = [item.id for item, _ in rows if item.visibility == Visibility.COMMON.value]

    def gated(external_processing: bool) -> set[uuid.UUID]:
        return set(db.scalars(select(LibraryItem.id).where(LibraryItem.id.in_(common), common_fulltext_items_clause(
            principal, external_processing=external_processing))).all()) if common else set()
    searchable, ragable = gated(False), gated(True)

    def available(item: LibraryItem, common_ok: set[uuid.UUID]) -> bool:
        return (item.id in common_ok if item.visibility == Visibility.COMMON.value
                else item.content_status == ContentStatus.INDEXED.value)
    return {"items": [{"id": str(item.id), "title": item.title, "visibility": item.visibility,
                       "content_status": item.content_status, "added_at": added.isoformat(),
                       "fulltext_search_available": available(item, searchable),
                       "rag_available": available(item, ragable)}
                      for item, added in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/{collection_id}/items", status_code=201)
def add_collection_item(collection_id: uuid.UUID, body: MembershipInput,
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    _owned(db, principal, collection_id, lock=True)
    item = db.scalar(select(LibraryItem).where(LibraryItem.id == body.item_id, visible_items_clause(principal)))
    if item is None:
        raise ApiError(404, "not_found")
    db.execute(insert(CollectionItem).values(collection_id=collection_id, item_id=body.item_id)
               .on_conflict_do_nothing(index_elements=["collection_id", "item_id"]))
    db.commit()
    return {"item_id": str(body.item_id)}


@router.delete("/{collection_id}/items/{item_id}", status_code=204)
def remove_collection_item(collection_id: uuid.UUID, item_id: uuid.UUID,
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> None:
    _owned(db, principal, collection_id, lock=True)
    result = db.execute(delete(CollectionItem).where(CollectionItem.collection_id == collection_id,
                                                      CollectionItem.item_id == item_id))
    if not result.rowcount:
        raise ApiError(404, "not_found")
    db.commit()
