"""Authenticated private full-text search."""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal
from app.authz import Principal
from app.config import Settings, get_settings
from app.search import open_chunk, search

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: Literal["keyword", "hybrid"] = "hybrid"
    item_id: uuid.UUID | None = None
    rerank: bool = True


@router.post("/search")
def search_route(body: SearchRequest, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return search(db, principal, settings, body.query, body.mode, body.item_id, body.rerank)


@router.get("/search/chunks/{chunk_id}")
def open_source_chunk(chunk_id: uuid.UUID, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)) -> dict:
    return open_chunk(db, principal, chunk_id)
