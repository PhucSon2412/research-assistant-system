"""Build private keyword/vector generations before atomically publishing them."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal, ROUND_UP

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest import markdown, pdf
from app.ingest.embedding import EmbeddingError, embed
from app.ingest.worker import StepError, _locked, _read_markdown, utcnow
from app.models import (Chunk, EmbeddingBudgetDay, EmbeddingCall, IndexEntry, IndexGeneration,
                        IngestionJob, IngestStepAttempt, LibraryItem, PaperVersion, PdfPage, ProcessingAcknowledgement,
                        Section, SourceFile, UploadIntent)
from app.models.enums import JobState, VerifiedMime
from app.storage import OriginalStorage

INDEX_VERSION = "private-keyword-vector-v1"


def _gate(db: Session, job_id: uuid.UUID, token: uuid.UUID):
    job, item, version, source = _locked(db, job_id, token)
    if (source.job_id != job.id or job.scope_owner_user_id != item.owner_user_id or
            version.coverage not in ("complete", "partial")):
        raise StepError("index_rights_or_coverage_invalid")
    acknowledged = db.scalar(select(UploadIntent.id).join(ProcessingAcknowledgement,
        UploadIntent.acknowledgement_id == ProcessingAcknowledgement.id).where(
        UploadIntent.item_id == item.id, UploadIntent.user_id == item.owner_user_id,
        UploadIntent.used_at.is_not(None),
        UploadIntent.session_id == ProcessingAcknowledgement.session_id,
        UploadIntent.declared_size == source.byte_size,
        UploadIntent.declared_sha256 == source.checksum_sha256,
        UploadIntent.file_kind == ("pdf" if source.verified_mime == VerifiedMime.PDF.value else "markdown"),
        ProcessingAcknowledgement.actor_user_id == item.owner_user_id,
        ProcessingAcknowledgement.notice_version == UploadIntent.notice_version))
    if acknowledged is None:
        raise StepError("upload_acknowledgement_missing")
    return job, item, version, source


def _eligible_chunks(db: Session, source: SourceFile, version: PaperVersion,
                     storage: OriginalStorage, settings: Settings) -> tuple[str, list[Chunk]]:
    pipeline = pdf.PIPELINE_VERSION if source.verified_mime == VerifiedMime.PDF.value else markdown.PIPELINE_VERSION
    chunks = db.scalars(select(Chunk).where(Chunk.source_file_id == source.id,
        Chunk.pipeline_version == pipeline).order_by(Chunk.section_id, Chunk.ordinal)).all()
    if not chunks or not any(db.get(Section, c.section_id).kind == "body" for c in chunks):
        raise StepError("index_body_missing")
    try:
        if storage.size(source.storage_key) != source.byte_size:
            raise StepError("index_original_mismatch")
    except StepError:
        raise
    except Exception:
        raise StepError("storage_unavailable", retryable=True) from None
    if source.verified_mime == VerifiedMime.PDF.value:
        pages = db.scalars(select(PdfPage).where(PdfPage.source_file_id == source.id,
            PdfPage.pipeline_version == pipeline)).all()
        if len(pages) != source.page_count or any(p.pipeline_version != pipeline for p in pages):
            raise StepError("index_pdf_layout_stale")
        body_pages = [p for p in pages if p.body_page]
        if (not body_pages or sum(p.qualifies for p in body_pages) / len(body_pages) < settings.pdf_body.min_body_page_ratio
                or sum(p.body_chars for p in body_pages) < settings.pdf_body.min_total_body_chars):
            raise StepError("index_pdf_coverage_invalid")
        source_text = {p.page_number: p.text for p in pages}
        page_by_number = {p.page_number: p for p in pages}
    else:
        source_text = _read_markdown(source, storage, settings)
    eligible = []
    for chunk in chunks:
        section = db.get(Section, chunk.section_id)
        if (section is None or section.pipeline_version != pipeline or section.item_id != chunk.item_id
                or section.version_id != version.id or chunk.item_id != version.item_id
                or chunk.version_id != version.id or chunk.source_file_id != source.id
                or chunk.chunk_hash != hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()):
            raise StepError("index_chunk_invalid")
        if section.kind == "references":
            continue
        if section.kind not in ("body", "abstract"):
            raise StepError("index_section_invalid")
        if source.verified_mime == VerifiedMime.PDF.value:
            detail = chunk.locator_detail or {}
            start, end = detail.get("start_offset"), detail.get("end_offset")
            page = chunk.start_page
            section_detail = section.locator_detail or {}
            if (chunk.locator_kind != "pdf" or page != chunk.end_page or page not in source_text
                    or detail.get("parser") != pipeline or detail.get("offset_unit") != "page_text_codepoint"
                    or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start
                    or source_text[page][start:end] != chunk.text
                    or section.start_page != page or section.end_page != page
                    or section_detail.get("parser") != pipeline
                    or source_text[page][section_detail.get("start_offset", -1):section_detail.get("end_offset", -1)] != section.text
                    or page_by_number[page].role in ("references", "bibliography_uncertain", "unextractable")
                    or (section.kind == "body" and not page_by_number[page].body_page)):
                raise StepError("index_locator_invalid")
        elif (chunk.locator_kind != "markdown" or chunk.start_offset is None or chunk.end_offset is None
              or source_text[chunk.start_offset:chunk.end_offset] != chunk.text
              or source_text[section.start_offset:section.end_offset] != section.text):
            raise StepError("index_locator_invalid")
        eligible.append(chunk)
    if not eligible:
        raise StepError("index_chunks_missing")
    return pipeline, eligible


def _amount(chunk: Chunk, settings: Settings) -> Decimal:
    # UTF-8 byte count is a conservative token ceiling for text inputs.
    ceiling = len(chunk.text.encode("utf-8"))
    price = Decimal(str(settings.embedding.max_usd_per_million_tokens))
    return (Decimal(ceiling) * price / Decimal(1_000_000)).quantize(Decimal("0.00000001"), rounding=ROUND_UP)


def _reserve(db: Session, source: SourceFile, chunk: Chunk, settings: Settings) -> EmbeddingCall:
    day = utcnow().date()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('ras_embedding_capacity'))"))
    db.execute(text("INSERT INTO embedding_budget_days(day) VALUES (:day) ON CONFLICT DO NOTHING"), {"day": day})
    budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == day).with_for_update())
    existing = db.scalar(select(EmbeddingCall).where(EmbeddingCall.source_file_id == source.id,
        EmbeddingCall.model_id == settings.embedding.model_id,
        EmbeddingCall.pipeline_version == chunk.pipeline_version, EmbeddingCall.chunk_hash == chunk.chunk_hash).with_for_update())
    if existing is not None:
        if existing.status == "completed":
            return existing
        if existing.status != "rejected":
            if existing.status == "reserved":
                existing.status = "uncertain"
                db.commit()
            raise StepError("embedding_outcome_uncertain")
    active = db.scalar(select(func.count()).select_from(EmbeddingCall).where(EmbeddingCall.status == "reserved"))
    if active >= settings.embedding.max_concurrent_requests:
        raise StepError("embedding_concurrency_limit", retryable=True)
    amount = _amount(chunk, settings)
    cap = min(Decimal(str(settings.budget.app_daily_usd)), Decimal(str(settings.budget.embedding_daily_usd)))
    if budget.committed_usd + budget.reserved_usd + amount > cap:
        raise StepError("embedding_budget_exhausted", retryable=True)
    budget.reserved_usd += amount
    if existing is None:
        existing = EmbeddingCall(source_file_id=source.id, model_id=settings.embedding.model_id,
            provider=settings.embedding.provider, pipeline_version=chunk.pipeline_version,
            chunk_hash=chunk.chunk_hash, status="reserved", reserved_usd=amount,
            cost_status="unknown", reserved_day=day)
        db.add(existing)
    else:
        existing.status = "reserved"
        existing.reserved_usd = amount
        existing.reserved_day = day
    db.flush()
    return existing


def process_index(db: Session, job_id: uuid.UUID, token: uuid.UUID,
                  storage: OriginalStorage, settings: Settings) -> str:
    while True:
        job, item, version, source = _gate(db, job_id, token)
        from datetime import timedelta
        job.lease_until = utcnow() + timedelta(seconds=settings.ingest.lease_seconds)
        pipeline, chunks = _eligible_chunks(db, source, version, storage, settings)
        generation = db.scalar(select(IndexGeneration).where(IndexGeneration.job_id == job.id,
            IndexGeneration.model_id == settings.embedding.model_id,
            IndexGeneration.index_version == INDEX_VERSION).with_for_update())
        if generation is None:
            generation = IndexGeneration(item_id=item.id, version_id=version.id, source_file_id=source.id,
                job_id=job.id, model_id=settings.embedding.model_id, provider=settings.embedding.provider,
                pipeline_version=pipeline, index_version=INDEX_VERSION, expected_chunks=len(chunks),
                rights_epoch=item.rights_epoch, state="building")
            db.add(generation)
            db.flush()
        elif (generation.state != "building" or generation.expected_chunks != len(chunks)
              or generation.pipeline_version != pipeline or generation.rights_epoch != item.rights_epoch):
            raise StepError("index_generation_changed")
        done = set(db.scalars(select(IndexEntry.chunk_id).where(IndexEntry.generation_id == generation.id)).all())
        pending = next((c for c in chunks if c.id not in done), None)
        if pending is None:
            _publish(db, job, item, generation, chunks, settings)
            db.commit()
            return JobState.READY.value
        if len(pending.text.encode("utf-8")) > settings.embedding.max_request_bytes:
            raise StepError("embedding_request_too_large")
        call = _reserve(db, source, pending, settings)
        if call.status == "completed":
            _insert_entry(db, generation, pending, call)
            db.commit()
            continue
        call_id = call.id
        db.commit()  # durable reservation before any external request
        try:
            # Fresh rights/lease/ack check immediately before the call.
            _gate(db, job_id, token)
            db.commit()
            result = embed(pending.text, settings)
        except EmbeddingError as exc:
            _provider_failure(db, call_id, exc)
            raise StepError(exc.code, retryable=exc.definitely_rejected) from None
        except StepError:
            _provider_failure(db, call_id, EmbeddingError("pre_call_guard_rejected", definitely_rejected=True))
            raise
        try:
            job, item, version, source = _gate(db, job_id, token)
            generation = db.get(IndexGeneration, generation.id)
            call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == call_id).with_for_update())
            if (generation.state != "building" or call.status != "reserved"
                    or generation.model_id != result.model_id
                    or (generation.dimensions is not None and generation.dimensions != len(result.vector))):
                raise StepError("embedding_dimensions_mismatch")
            generation.dimensions = len(result.vector)
            call.vector = result.vector
            call.dimensions = len(result.vector)
            call.input_tokens = result.input_tokens
            call.status = "completed"
            call.cost_status = "unknown_reserved_ceiling"
            call.finished_at = utcnow()
            budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == call.reserved_day).with_for_update())
            budget.reserved_usd -= call.reserved_usd
            budget.committed_usd += call.reserved_usd  # unknown bill: retain full ceiling
            _insert_entry(db, generation, pending, call)
            db.commit()
        except Exception:
            db.rollback()
            # A response may have been billed. Never automatically call it again.
            _provider_failure(db, call_id, EmbeddingError("embedding_outcome_uncertain"))
            raise


def _provider_failure(db: Session, call_id: uuid.UUID, error: EmbeddingError) -> None:
    db.rollback()
    call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == call_id).with_for_update())
    if call is not None and call.status == "reserved":
        call.status = "rejected" if error.definitely_rejected else "uncertain"
        call.finished_at = utcnow()
        if error.definitely_rejected:
            call.cost_status = "rejected_no_charge"
            call.cost_usd = Decimal("0")
            budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == call.reserved_day).with_for_update())
            budget.reserved_usd -= call.reserved_usd
    db.commit()


def _insert_entry(db: Session, generation: IndexGeneration, chunk: Chunk, call: EmbeddingCall) -> None:
    if call.status != "completed" or call.dimensions is None:
        raise StepError("embedding_incomplete")
    if generation.dimensions is not None and generation.dimensions != call.dimensions:
        raise StepError("embedding_dimensions_mismatch")
    generation.dimensions = call.dimensions
    db.add(IndexEntry(generation_id=generation.id, chunk_id=chunk.id, embedding_call_id=call.id,
                      chunk_hash=chunk.chunk_hash, text=chunk.text))


def _publish(db: Session, job: IngestionJob, item: LibraryItem, generation: IndexGeneration,
             chunks: list[Chunk], settings: Settings) -> None:
    entries = db.scalars(select(IndexEntry).where(IndexEntry.generation_id == generation.id)).all()
    if (generation.dimensions is None or len(entries) != generation.expected_chunks
            or {e.chunk_id for e in entries} != {c.id for c in chunks}):
        raise StepError("index_incomplete")
    for entry in entries:
        call = db.get(EmbeddingCall, entry.embedding_call_id)
        if (call.status != "completed" or call.vector is None or call.dimensions != generation.dimensions
                or call.model_id != generation.model_id or entry.chunk_hash != call.chunk_hash):
            raise StepError("index_vector_mismatch")
    if item.active_index_generation_id is not None:
        previous = db.get(IndexGeneration, item.active_index_generation_id)
        previous.state = "superseded"
    generation.state = "active"
    generation.activated_at = utcnow()
    item.active_index_generation_id = generation.id
    item.active_version_id = generation.version_id
    item.content_status = "indexed"
    job.state = JobState.READY.value
    job.finished_at = generation.activated_at
    job.updated_at = generation.activated_at
    job.lease_token = None
    job.lease_until = None
    job.resume_state = None
    job.last_error_code = None
    attempt = db.scalar(select(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
        IngestStepAttempt.step == "index", IngestStepAttempt.attempt_no == job.attempt_count))
    if attempt is not None:
        attempt.outcome = "success"
        attempt.finished_at = generation.activated_at
