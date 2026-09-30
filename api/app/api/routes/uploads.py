"""Private two-step uploads (M1c) and ACL-gated original downloads (M1c private, M6b-2b1 common)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_db, get_now, get_principal
from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, common_original_clause, m2a_private_derivatives_clause
from app.common_original import VerifiedFileResponse, prepare_common_original
from app.config import Settings, get_settings
from app.identity.tokens import generate_token, hash_token, plausible_token
from app.models import IngestionJob, LibraryItem, PaperVersion, ProcessingAcknowledgement, SourceFile, UploadIntent
from app.models.enums import AuditActorType, AuditOutcome, JobOrigin, JobState, VerifiedMime, Visibility
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
    acknowledged: Literal[True]
    notice_version: str
    file_kind: Literal["pdf", "markdown"]
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _storage(settings: Settings = Depends(get_settings)) -> OriginalStorage:
    return get_storage(settings)


@router.get("/uploads/notice")
def notice(principal: Principal = Depends(get_principal), settings: Settings = Depends(get_settings)) -> dict:
    return {"version": settings.upload_notice_version, "text": NOTICE}


@router.post("/uploads/intents", status_code=201)
def create_intent(
    body: IntentInput,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
) -> dict:
    if request.headers.get("origin") not in settings.http.allowed_origins:
        raise ApiError(403, "csrf_failed")
    if principal.is_admin:
        raise ApiError(403, "forbidden")
    if body.notice_version != settings.upload_notice_version:
        raise ApiError(409, "notice_changed")
    if Path(body.filename).name != body.filename or "\\" in body.filename or any(ord(c) < 32 for c in body.filename):
        raise ApiError(422, "invalid_filename")
    suffixes = (".pdf",) if body.file_kind == "pdf" else (".md", ".markdown")
    if not body.filename.lower().endswith(suffixes):
        raise ApiError(422, "invalid_filename")
    limit = settings.files.pdf_max_bytes if body.file_kind == "pdf" else settings.files.markdown_max_bytes
    if body.byte_size > limit:
        raise ApiError(413, "file_too_large")
    raw, digest = generate_token()
    ack = ProcessingAcknowledgement(
        actor_user_id=principal.user_id,
        session_id=principal.session_id,
        notice_version=body.notice_version,
        acknowledged_at=now,
    )
    db.add(ack)
    db.flush()
    intent = UploadIntent(
        token_hash=digest,
        acknowledgement_id=ack.id,
        user_id=principal.user_id,
        session_id=principal.session_id,
        notice_version=body.notice_version,
        file_kind=body.file_kind,
        filename=body.filename,
        declared_size=body.byte_size,
        declared_sha256=body.sha256,
        created_at=now,
        expires_at=now + settings.files.upload_intent_lifetime,
    )
    db.add(intent)
    db.flush()
    record_event(db, action="upload.intent.create", outcome=AuditOutcome.SUCCESS,
                 actor_type=AuditActorType.USER, actor_user_id=principal.user_id,
                 target_type="upload_intent", target_id=str(intent.id),
                 details={"notice_version": body.notice_version})
    db.commit()
    return {"intent_token": raw, "expires_at": intent.expires_at.isoformat()}


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


def _delete_if_unreferenced(db: Session, storage: OriginalStorage, key: str) -> None:
    """A failed/ambiguous commit may have succeeded: never delete a referenced object."""
    try:
        db.rollback()
        if db.scalar(select(SourceFile.id).where(SourceFile.storage_key == key)) is None:
            storage.delete(key)
    except Exception:
        # An unreachable DB makes the commit outcome unknown. A later orphan
        # sweep removes unreferenced objects; no object URL is exposed.
        pass


@router.put("/uploads/original", status_code=201)
async def upload_original(
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
    storage: OriginalStorage = Depends(_storage),
) -> dict:
    if request.headers.get("origin") not in settings.http.allowed_origins:
        raise ApiError(403, "csrf_failed")
    raw = request.headers.get("x-upload-intent")
    if not plausible_token(raw):
        raise ApiError(404, "invalid_intent")
    # Lock for the entire bounded transfer. A racing request waits and then
    # sees used_at; only one transaction can publish the item.
    intent = db.scalar(select(UploadIntent).where(UploadIntent.token_hash == hash_token(raw)).with_for_update())
    if (intent is None or intent.user_id != principal.user_id or intent.session_id != principal.session_id
            or intent.notice_version != settings.upload_notice_version or intent.used_at is not None
            or now >= intent.expires_at):
        db.rollback()
        raise ApiError(404, "invalid_intent")
    limit = settings.files.pdf_max_bytes if intent.file_kind == "pdf" else settings.files.markdown_max_bytes
    if request.headers.get("content-type", "").split(";")[0] not in ("application/octet-stream", "application/pdf", "text/markdown"):
        db.rollback()
        raise ApiError(422, "invalid_file_type")
    if request.headers.get("content-length"):
        try:
            declared_length = int(request.headers["content-length"])
        except ValueError:
            db.rollback()
            raise ApiError(422, "invalid_request") from None
        if declared_length > limit or declared_length > intent.declared_size:
            db.rollback()
            raise ApiError(413, "file_too_large")
    key = f"originals/{uuid.uuid4().hex}"
    stored = False
    try:
        with tempfile.TemporaryDirectory(prefix="ras-upload-") as directory:
            path = os.path.join(directory, "original")
            digest = hashlib.sha256()
            size = 0
            with open(path, "wb") as file:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit or size > intent.declared_size:
                        raise ApiError(413, "file_too_large")
                    digest.update(chunk)
                    file.write(chunk)
            if size != intent.declared_size or digest.hexdigest() != intent.declared_sha256:
                raise ApiError(422, "file_mismatch")
            mime, pages = await run_in_threadpool(_validate_file, path, intent.file_kind, settings)
            with open(path, "rb") as file:
                await run_in_threadpool(storage.put, key, file, mime)
            stored = True
        item = LibraryItem(visibility=Visibility.PRIVATE.value, owner_user_id=principal.user_id,
                           created_by_user_id=principal.user_id, title=Path(intent.filename).stem)
        db.add(item)
        db.flush()
        version = PaperVersion(item_id=item.id)
        db.add(version)
        db.flush()
        job = IngestionJob(origin=JobOrigin.UPLOAD.value, actor_user_id=principal.user_id,
                           scope_visibility=Visibility.PRIVATE.value, scope_owner_user_id=principal.user_id,
                           item_id=item.id, version_id=version.id, idempotency_key=str(intent.id),
                           state=JobState.RECEIVED.value, rights_epoch_at_start=0)
        db.add(job)
        db.flush()
        source = SourceFile(item_id=item.id, version_id=version.id, job_id=job.id, storage_key=key,
                            verified_mime=mime, byte_size=size, page_count=pages,
                            checksum_sha256=digest.hexdigest(), original_filename=intent.filename,
                            file_source="upload")
        db.add(source)
        intent.used_at = now
        intent.item_id = item.id
        db.flush()
        record_event(db, action="upload.original.receive", outcome=AuditOutcome.SUCCESS,
                     actor_type=AuditActorType.USER, actor_user_id=principal.user_id,
                     target_type="library_item", target_id=str(item.id))
        db.commit()
        return {"item_id": str(item.id), "version_id": str(version.id), "file_id": str(source.id),
                "job_id": str(job.id), "state": "received"}
    except ApiError:
        db.rollback()
        raise
    except Exception:
        if stored:
            _delete_if_unreferenced(db, storage, key)
        else:
            db.rollback()
            try:
                storage.delete(key)  # multipart/partial put may have left an object
            except Exception:
                pass
        raise ApiError(503, "upload_unavailable") from None


@router.get("/files/{file_id}/original")
def download_original(
    file_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    clock: Callable[[], datetime] = Depends(get_clock),
    settings: Settings = Depends(get_settings),
    storage: OriginalStorage = Depends(_storage),
) -> StreamingResponse:
    """The owner's private original, or a published common original its AccessRights open (M6b-2b1).

    Forbidden, pending, revoked, unrighted, wrong-file and missing IDs all answer
    the same ``404``. The common branch verifies the bytes and re-checks rights
    before the first byte (``app.common_original``); no object URL is issued.
    """
    try:
        parsed = uuid.UUID(file_id)
    except ValueError:
        raise ApiError(404, "not_found") from None
    row = db.execute(
        select(SourceFile, LibraryItem.visibility, LibraryItem.rights_epoch)
        .join(PaperVersion, SourceFile.version_id == PaperVersion.id)
        .join(LibraryItem, PaperVersion.item_id == LibraryItem.id)
        .where(SourceFile.id == parsed,
               or_(m2a_private_derivatives_clause(principal), common_original_clause(principal)))
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    source, visibility, rights_epoch = row
    filename = quote(source.original_filename or "original", safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Content-Length": str(source.byte_size),
        "Cache-Control": "no-store",
    }
    if visibility == Visibility.COMMON.value:
        verified = prepare_common_original(db, principal, storage, source, rights_epoch, clock=clock,
                                           settings=settings)
        return VerifiedFileResponse(verified, media_type=source.verified_mime, headers=headers)
    try:
        body = storage.open(source.storage_key)
    except Exception:
        raise ApiError(503, "file_unavailable") from None

    def chunks():
        try:
            yield from body.iter_chunks(chunk_size=64 * 1024)
        finally:
            body.close()

    return StreamingResponse(chunks(), media_type=source.verified_mime, headers=headers)
