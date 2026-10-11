"""Authenticated full-text search: the caller's private items and servable common copies (M6b-2b3a).

HTTP and RAG share short runtime retrieval checkpoints. Generation has its own
content orchestration and nonce-bound financial attempt.
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from pydantic import model_validator
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_db, get_principal
from app.api.cookies import CSRF_HEADER
from app.api.errors import ApiError
from app.authz import Principal
from app.config import Settings, get_settings
from app import search_boundary, search_runtime

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: Literal["keyword", "hybrid"] = "hybrid"
    item_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    rerank: bool = False

    @model_validator(mode="after")
    def one_scope(self):
        if self.item_id is not None and self.collection_id is not None:
            raise ValueError("item_id and collection_id are mutually exclusive")
        return self


@router.post("/search")
def search_route(body: SearchRequest, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                 clock=Depends(get_clock)) -> dict:
    engine = db.get_bind()
    search_boundary.release(db)
    try:
        return search_runtime.search(engine, principal, settings, body.query, body.mode, body.item_id,
            body.rerank, body.collection_id, clock=clock, csrf=request.headers.get(CSRF_HEADER) or '')
    except ApiError:
        raise
    except Exception:
        # Unexpected provider/DTO failure keeps the reservation ceiling and never retries.
        raise ApiError(503, 'search_checkpoint_unavailable') from None


@router.get("/search/chunks/{chunk_id}")
def open_source_chunk(chunk_id: uuid.UUID, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                      clock=Depends(get_clock)) -> dict:
    engine = db.get_bind()
    search_boundary.release(db)
    return search_runtime.open_chunk(engine, principal, settings, chunk_id, clock=clock)
