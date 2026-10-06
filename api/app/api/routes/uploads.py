"""Private two-step uploads (M1c) and ACL-gated original downloads (M1c private, M6b-2b1 common)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_db, get_principal, require_trusted_origin
from app.api import upload_intents
from app.api import upload_original as original_upload
from app.api.errors import ApiError
from app.authz import Principal
from app.api import original_downloads
from app.config import Settings, get_settings
from app.identity.final_caller import final_caller
from app.models.enums import VerifiedMime
from app.pdf_probe import page_count
from app.storage import OriginalStorage, get_storage

router = APIRouter(tags=["uploads"])

NOTICE = (
    "Toàn bộ text trích xuất từ file sẽ được gửi qua OpenRouter/provider để embedding. "
    "Candidate chunk có thể được gửi để rerank. Câu hỏi và các chunk được chọn "
    "có thể được gửi khi RAG."
)


class IntentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged: StrictBool
    notice_version: str
    file_kind: Literal["pdf", "markdown"]
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("acknowledged")
    @classmethod
    def explicit_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("explicit confirmation required")
        return value


def _storage(settings: Settings = Depends(get_settings)) -> OriginalStorage:
    return get_storage(settings)


@router.get("/uploads/notice")
def notice(principal: Principal = Depends(get_principal), settings: Settings = Depends(get_settings),
           db: Session = Depends(get_db), clock=Depends(get_clock)) -> dict:
    try:
        final_caller(db, session_id=principal.session_id, user_id=principal.user_id,
                     clock=clock, idle=settings.auth.session_idle)
        return {"version": settings.upload_notice_version, "text": NOTICE}
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, "service_recovering") from None
    finally:
        db.rollback()


@router.post("/uploads/intents", status_code=201)
def create_intent(
    body: IntentInput,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    clock: Callable[[], datetime] = Depends(get_clock),
    settings: Settings = Depends(get_settings),
    _origin: None = Depends(require_trusted_origin),
) -> dict:
    return upload_intents.create(db, principal, body, csrf_token=request.headers.get("x-csrf-token") or "",
                                 clock=clock, settings=settings)


def _validate_file(path: str, kind: str, settings: Settings) -> tuple[str, int | None]:
    if kind == "pdf":
        pages = page_count(path, settings.files)
        if pages is None:
            raise ApiError(422, "invalid_pdf")
        return VerifiedMime.PDF.value, pages
    try:
        with open(path, "rb") as file:
            data = file.read()
        text = data.decode("utf-8", errors="strict")
        if not text.strip() or "\x00" in text or any(ord(c) < 32 and c not in "\t\n\r" for c in text):
            raise ValueError("binary or empty")
    except (UnicodeError, ValueError):
        raise ApiError(422, "invalid_markdown") from None
    return VerifiedMime.MARKDOWN.value, None


@router.put("/uploads/original", status_code=201)
async def upload_original(
    request: Request,
    principal: Principal = Depends(get_principal),
    clock: Callable[[], datetime] = Depends(get_clock),
    settings: Settings = Depends(get_settings),
    storage: OriginalStorage = Depends(_storage),
    _origin: None = Depends(require_trusted_origin),
) -> dict:
    return await original_upload.receive(request, principal, clock, settings, storage, _validate_file)


@router.get("/files/{file_id}/original")
async def download_original(
    file_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    clock: Callable[[], datetime] = Depends(get_clock),
    settings: Settings = Depends(get_settings),
    storage: OriginalStorage = Depends(_storage),
) -> StreamingResponse:
    return await original_downloads.receive(request, principal, file_id, clock, settings, storage)
