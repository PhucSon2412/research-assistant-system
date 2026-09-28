"""Authenticated single-paper RAG."""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal
from app.authz import Principal
from app.config import Settings, get_settings
from app.rag import answer

router = APIRouter(tags=["rag"])


class RagRequest(BaseModel):
    item_id: uuid.UUID
    question: str = Field(min_length=1)
    mode: Literal["keyword", "hybrid"] = "keyword"


@router.post("/rag/ask")
def ask(body: RagRequest, principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return answer(db, principal, settings, body.item_id, body.question, body.mode)
