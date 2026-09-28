"""Authenticated, mutually exclusive RAG scopes."""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal
from app.authz import Principal
from app.config import Settings, get_settings
from app.rag import answer

router = APIRouter(tags=["rag"])


class RagRequest(BaseModel):
    item_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    scope: Literal["all"] | None = None
    question: str = Field(min_length=1)
    mode: Literal["keyword", "hybrid"] = "keyword"

    @model_validator(mode="after")
    def one_scope(self) -> "RagRequest":
        if sum(value is not None for value in (self.item_id, self.collection_id, self.scope)) != 1:
            raise ValueError("choose exactly one RAG scope")
        return self


@router.post("/rag/ask")
def ask(body: RagRequest, principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return answer(db, principal, settings, body.item_id, body.question, body.mode,
                  collection_id=body.collection_id, all_items=body.scope == "all")
