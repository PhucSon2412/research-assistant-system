"""Build keyword/vector generations before atomically publishing them.

Private uploads (M3a) and, since M6b-2b2b, published reviewed common copies
(``promotion`` jobs) use the same steps. For a common copy the rights gate of
``app.common_index`` replaces the upload acknowledgement and runs, with the
item locked, before every reservation, immediately before each provider call,
after each response and in the publishing transaction.

No storage I/O happens while the item and job are locked (review PR #23): a
revoke never waits on a slow object read. The whole object is read and its
size and SHA-256 checked, with no lock held, before every provider call and
before the publishing transaction (the read that opens a run serves the first
call); only then are the job, lease, cancel flag, rights, epoch and tombstone
checked under the lock and committed before the call, so no lock is held while
the provider works either. A HEAD of size/ETag/Last-Modified is used only to
reject early (around each full read and after each response); it never
replaces the hash. Between the last hash and the call there are only DB
steps: an object replaced in that gap is not seen before that call, but the
next hash (before the next call or before publishing) refuses it. A common copy's calls are charged to
the application and embedding buckets only (``owner_user_id`` NULL), never to a
User's quota. Publishing makes the copy ``indexed``, which opens nothing yet:
chunk, search and RAG routes keep the private-owner gate.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_UP

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest import markdown, pdf
from app.ingest.embedding import EmbeddingError, embed
from app.ingest.worker import StepError, _locked, _read_markdown, _read_pdf, utcnow
from app.models import (Chunk, EmbeddingBudgetDay, EmbeddingCall, EmbeddingUserBudgetDay, IndexEntry, IndexGeneration,
                        IngestionJob, IngestStepAttempt, LibraryItem, PaperVersion, PdfPage, ProcessingAcknowledgement,
                        SearchUsage, Section, SourceFile, UploadIntent)
from app.models.enums import JobOrigin, JobState, VerifiedMime
from app.provider_capacity import LOCK, lease_deadline, reap_expired, reservation_heartbeat
from app.storage import OriginalStorage, is_missing_object

INDEX_VERSION = "private-keyword-vector-v1"
COMMON_INDEX_VERSION = "common-keyword-vector-v1"


def index_version(job: IngestionJob) -> str:
    return COMMON_INDEX_VERSION if job.origin == JobOrigin.PROMOTION.value else INDEX_VERSION


def _gate(db: Session, job_id: uuid.UUID, token: uuid.UUID):
    job, item, version, source = _locked(db, job_id, token)
    if version.coverage not in ("complete", "partial"):
        raise StepError("index_rights_or_coverage_invalid")
    if job.origin == JobOrigin.PROMOTION.value:
        # _locked already re-checked the common rights gate (full text + external processing,
        # no conditions, live publication, epoch) with the item locked.
        if source.id != job.source_file_id or job.scope_owner_user_id is not None or item.owner_user_id is not None:
            raise StepError("index_rights_or_coverage_invalid")
        return job, item, version, source
    if source.job_id != job.id or job.scope_owner_user_id != item.owner_user_id:
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


@dataclass(frozen=True)
class VerifiedObject:
    """The stored object as last hashed: its fingerprint, and the decoded text of a Markdown file."""

    fingerprint: tuple
    text: str | None


def _fingerprint(storage: OriginalStorage, source: SourceFile) -> tuple:
    try:
        return storage.fingerprint(source.storage_key)
    except Exception as exc:
        if is_missing_object(exc):
            raise StepError("checksum_mismatch") from None
        raise StepError("storage_unavailable", retryable=True) from None


def _verify_object(source: SourceFile, storage: OriginalStorage, settings: Settings) -> VerifiedObject:
    """Full size + SHA-256 of the stored object, including same-sized replacements.

    Called with no DB lock held. A fingerprint that changes during the read also refuses
    (early reject only; the hash is the check).
    """
    before = _fingerprint(storage, source)
    if source.verified_mime == VerifiedMime.PDF.value:
        _read_pdf(source, storage, settings)
        text_value = None
    else:
        text_value = _read_markdown(source, storage, settings)
    if _fingerprint(storage, source) != before:
        raise StepError("checksum_mismatch")
    return VerifiedObject(before, text_value)


def _check_unchanged(source: SourceFile, storage: OriginalStorage, verified: VerifiedObject) -> None:
    """Early reject by HEAD: refuses a fingerprint that moved since the last hash. It cannot prove the
    bytes unchanged (see ``OriginalStorage.fingerprint``), so it never stands in for ``_verify_object``.
    No DB lock held."""
    if _fingerprint(storage, source) != verified.fingerprint:
        raise StepError("checksum_mismatch")


def _eligible_chunks(db: Session, source: SourceFile, version: PaperVersion,
                     verified: VerifiedObject, settings: Settings) -> tuple[str, list[Chunk]]:
    """Chunks to index, checked against the extracted pages or the verified Markdown text. No storage I/O."""
    pipeline = pdf.PIPELINE_VERSION if source.verified_mime == VerifiedMime.PDF.value else markdown.PIPELINE_VERSION
    chunks = db.scalars(select(Chunk).where(Chunk.source_file_id == source.id,
        Chunk.pipeline_version == pipeline).order_by(Chunk.section_id, Chunk.ordinal)).all()
    if not chunks or not any(db.get(Section, c.section_id).kind == "body" for c in chunks):
        raise StepError("index_body_missing")
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
        source_text = verified.text
        if source_text is None:
            raise StepError("index_object_unverified")
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


def _user_budget(db: Session, day, user_id: uuid.UUID | None) -> EmbeddingUserBudgetDay | None:
    """The User's daily quota row, locked; ``None`` for a common copy's call (application only)."""
    if user_id is None:
        return None
    return db.scalar(select(EmbeddingUserBudgetDay).where(
        EmbeddingUserBudgetDay.day == day, EmbeddingUserBudgetDay.user_id == user_id).with_for_update())


def _amount(tokens: int, settings: Settings) -> Decimal:
    price = Decimal(str(settings.embedding.max_usd_per_million_tokens))
    return (Decimal(tokens) * price / Decimal(1_000_000)).quantize(Decimal("0.00000001"), rounding=ROUND_UP)


def _reserve(db: Session, job: IngestionJob, item: LibraryItem, source: SourceFile,
             chunk: Chunk, settings: Settings) -> EmbeddingCall:
    day = utcnow().date()
    reap_expired(db.get_bind())
    db.execute(text(LOCK))
    db.execute(text("INSERT INTO embedding_budget_days(day) VALUES (:day) ON CONFLICT DO NOTHING"), {"day": day})
    budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == day).with_for_update())
    # A common copy (no owner) is charged to the application and embedding buckets only.
    if item.owner_user_id is not None:
        db.execute(text("INSERT INTO embedding_user_budget_days(day,user_id) VALUES (:day,:user) ON CONFLICT DO NOTHING"),
                   {"day": day, "user": item.owner_user_id})
    user_budget = _user_budget(db, day, item.owner_user_id)
    existing = db.scalar(select(EmbeddingCall).where(EmbeddingCall.source_file_id == source.id,
        EmbeddingCall.model_id == settings.embedding.model_id,
        EmbeddingCall.pipeline_version == chunk.pipeline_version, EmbeddingCall.chunk_hash == chunk.chunk_hash).with_for_update())
    if existing is not None:
        if existing.status == "completed":
            return existing
        if existing.status != "rejected":
            if existing.status == "reserved":
                raise StepError("embedding_in_progress", retryable=True)
            raise StepError("embedding_outcome_uncertain")
    active = db.scalar(select(func.count()).select_from(EmbeddingCall).where(EmbeddingCall.status == "reserved"))
    active_queries = db.scalar(select(func.count()).select_from(SearchUsage).where(
        SearchUsage.task_type == "embed_query", SearchUsage.status == "reserved"))
    if active + active_queries >= settings.embedding.max_concurrent_requests:
        raise StepError("embedding_concurrency_limit", retryable=True)
    if not settings.embedding.pricing_verified:
        raise StepError("embedding_pricing_unverified", retryable=True)
    # Reserve the full configured request limit. The provider's token count is
    # reconciled later; concurrent requests cannot spend the same allowance.
    amount = _amount(settings.embedding.max_request_bytes, settings)
    cap = min(Decimal(str(settings.budget.app_daily_usd)), Decimal(str(settings.budget.embedding_daily_usd)))
    search_spend = db.scalar(select(func.coalesce(func.sum(func.coalesce(SearchUsage.cost_usd, SearchUsage.reserved_usd)), 0)).where(
        SearchUsage.day == day, SearchUsage.status.in_(("reserved", "completed", "uncertain"))))
    query_spend = db.scalar(select(func.coalesce(func.sum(func.coalesce(SearchUsage.cost_usd, SearchUsage.reserved_usd)), 0)).where(
        SearchUsage.day == day, SearchUsage.task_type == "embed_query",
        SearchUsage.status.in_(("reserved", "completed", "uncertain"))))
    if budget.committed_usd + budget.reserved_usd + search_spend + amount > Decimal(str(settings.budget.app_daily_usd)):
        raise StepError("embedding_budget_exhausted", retryable=True)
    if budget.committed_usd + budget.reserved_usd + query_spend + amount > cap:
        raise StepError("embedding_budget_exhausted", retryable=True)
    user_cap = Decimal(str(settings.budget.user_embedding_daily_usd))
    user_query_spend = Decimal(0) if user_budget is None else db.scalar(select(func.coalesce(func.sum(func.coalesce(SearchUsage.cost_usd, SearchUsage.reserved_usd)), 0)).where(
        SearchUsage.day == day, SearchUsage.user_id == item.owner_user_id,
        SearchUsage.task_type == "embed_query", SearchUsage.status.in_(("reserved", "completed", "uncertain"))))
    if user_budget is not None and user_budget.committed_usd + user_budget.reserved_usd + user_query_spend + amount > user_cap:
        raise StepError("embedding_user_quota_exhausted", retryable=True)
    budget.reserved_usd += amount
    if user_budget is not None:
        user_budget.reserved_usd += amount
    if (budget.committed_usd + budget.reserved_usd + query_spend >=
            cap * Decimal(str(settings.budget.embedding_warning_fraction))):
        job.warnings = list(dict.fromkeys([*job.warnings, "embedding_bucket_near_limit"]))
    if user_budget is not None and (user_budget.committed_usd + user_budget.reserved_usd + user_query_spend >=
            user_cap * Decimal(str(settings.budget.user_embedding_warning_fraction))):
        job.warnings = list(dict.fromkeys([*job.warnings, "embedding_user_quota_near_limit"]))
    if existing is None:
        existing = EmbeddingCall(source_file_id=source.id, owner_user_id=item.owner_user_id,
            model_id=settings.embedding.model_id,
            provider=settings.embedding.provider, pipeline_version=chunk.pipeline_version,
            chunk_hash=chunk.chunk_hash, status="reserved", reserved_usd=amount,
            cost_status="unknown", reserved_day=day,
            lease_until=lease_deadline(settings.embedding.reservation_lease_seconds))
        db.add(existing)
    else:
        existing.status = "reserved"
        existing.reserved_usd = amount
        existing.reserved_day = day
        existing.cost_usd = None
        existing.cost_status = "unknown"
        existing.response_model_id = None
        existing.response_provider = None
        existing.finished_at = None
        existing.lease_until = lease_deadline(settings.embedding.reservation_lease_seconds)
    db.flush()
    return existing


def _verified_unlocked(db: Session, job_id: uuid.UUID, token: uuid.UUID, storage: OriginalStorage,
                       settings: Settings) -> VerifiedObject:
    """Check the gate, commit (releasing item and job), then read and hash the object with no lock held."""
    job, _, _, source = _gate(db, job_id, token)
    job.lease_until = utcnow() + timedelta(seconds=settings.ingest.lease_seconds)
    db.commit()
    return _verify_object(source, storage, settings)


def process_index(db: Session, job_id: uuid.UUID, token: uuid.UUID,
                  storage: OriginalStorage, settings: Settings) -> str:
    # Full SHA-256 read with no lock held; it serves the first provider call. Every later call and the
    # publishing transaction need a fresh one: the loop never reads the object while locked.
    verified = _verified_unlocked(db, job_id, token, storage, settings)
    hashed_since_call = True
    while True:
        job, item, version, source = _gate(db, job_id, token)
        job.lease_until = utcnow() + timedelta(seconds=settings.ingest.lease_seconds)
        pipeline, chunks = _eligible_chunks(db, source, version, verified, settings)
        generation = db.scalar(select(IndexGeneration).where(IndexGeneration.job_id == job.id,
            IndexGeneration.model_id == settings.embedding.model_id,
            IndexGeneration.index_version == index_version(job)).with_for_update())
        if generation is None:
            generation = IndexGeneration(item_id=item.id, version_id=version.id, source_file_id=source.id,
                job_id=job.id, model_id=settings.embedding.model_id, provider=settings.embedding.provider,
                pipeline_version=pipeline, index_version=index_version(job), expected_chunks=len(chunks),
                rights_epoch=item.rights_epoch, state="building")
            db.add(generation)
            db.flush()
        elif (generation.state != "building" or generation.expected_chunks != len(chunks)
              or generation.pipeline_version != pipeline or generation.rights_epoch != item.rights_epoch):
            raise StepError("index_generation_changed")
        done = set(db.scalars(select(IndexEntry.chunk_id).where(IndexEntry.generation_id == generation.id)).all())
        pending = next((c for c in chunks if c.id not in done), None)
        if pending is None and not hashed_since_call:
            # Hash the object again before publishing, outside the lock; a revoke meanwhile wins
            # at the next gate. The publishing transaction itself does no storage I/O.
            db.commit()
            verified = _verified_unlocked(db, job_id, token, storage, settings)
            hashed_since_call = True
            continue
        if pending is None:
            _publish(db, job, item, generation, chunks, settings)
            db.commit()
            return JobState.READY.value
        if len(pending.text.encode("utf-8")) > settings.embedding.max_request_bytes:
            raise StepError("embedding_request_too_large")
        call = _reserve(db, job, item, source, pending, settings)
        if call.status == "completed":
            _insert_entry(db, generation, pending, call)
            db.commit()
            continue
        call_id = call.id
        db.commit()  # durable reservation before any external request
        try:
            # Full SHA-256 of the object since the last call (no DB lock held), then a fresh
            # rights/lease/ack check under the item lock, committed before the call: no lock is held
            # while the provider works.
            if not hashed_since_call:
                verified = _verify_object(source, storage, settings)
            _, _, _, source = _gate(db, job_id, token)
            db.commit()
            hashed_since_call = False
            with reservation_heartbeat(db.get_bind(), EmbeddingCall, call_id,
                                       settings.embedding.reservation_lease_seconds):
                result = embed(pending.text, settings)
        except EmbeddingError as exc:
            _provider_failure(db, call_id, exc)
            raise StepError(exc.code, retryable=exc.definitely_rejected) from None
        except StepError:
            _provider_failure(db, call_id, EmbeddingError("pre_call_guard_rejected", definitely_rejected=True))
            raise
        try:
            _check_unchanged(source, storage, verified)  # early reject only; the next call re-hashes
            job, item, version, source = _gate(db, job_id, token)
            generation = db.get(IndexGeneration, generation.id)
            call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == call_id).with_for_update())
            if (generation.state != "building" or
                    (call.status != "reserved" and not (call.status == "uncertain" and
                     call.cost_status == "lease_expired_reserved_ceiling"))
                    or generation.model_id != result.model_id
                    or (generation.dimensions is not None and generation.dimensions != len(result.vector))):
                raise StepError("embedding_dimensions_mismatch")
            generation.dimensions = len(result.vector)
            call.vector = result.vector
            call.dimensions = len(result.vector)
            call.input_tokens = result.input_tokens
            call.response_model_id = result.response_model_id
            call.response_provider = result.response_provider
            call.status = "completed"
            if result.input_tokens is not None and result.input_tokens > settings.embedding.max_request_bytes:
                raise StepError("embedding_usage_exceeds_reservation")
            settled = (_amount(result.input_tokens, settings) if result.input_tokens is not None
                       else call.reserved_usd)
            call.cost_usd = settled if result.input_tokens is not None else None
            call.cost_status = "usage_price_estimate" if result.input_tokens is not None else "unknown_reserved_ceiling"
            call.finished_at = utcnow()
            call.lease_until = None
            budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == call.reserved_day).with_for_update())
            budget.reserved_usd -= call.reserved_usd
            budget.committed_usd += settled
            user_budget = _user_budget(db, call.reserved_day, call.owner_user_id)
            if user_budget is not None:
                user_budget.reserved_usd -= call.reserved_usd
                user_budget.committed_usd += settled
            _insert_entry(db, generation, pending, call)
            db.commit()
        except Exception:
            db.rollback()
            # A response may have been billed. Never automatically call it again.
            _provider_failure(db, call_id, EmbeddingError("embedding_outcome_uncertain",
                response_model_id=result.response_model_id,
                response_provider=result.response_provider))
            raise


def _provider_failure(db: Session, call_id: uuid.UUID, error: EmbeddingError) -> None:
    db.rollback()
    db.execute(text(LOCK))
    call = db.scalar(select(EmbeddingCall).where(EmbeddingCall.id == call_id).with_for_update())
    if (call is not None and (call.status == "reserved" or
            (call.status == "uncertain" and call.cost_status == "lease_expired_reserved_ceiling"))):
        call.response_model_id = error.response_model_id
        call.response_provider = error.response_provider
        call.status = "rejected" if error.definitely_rejected else "uncertain"
        call.finished_at = utcnow()
        call.lease_until = None
        if error.definitely_rejected:
            call.cost_status = "rejected_no_charge"
            call.cost_usd = Decimal("0")
            budget = db.scalar(select(EmbeddingBudgetDay).where(EmbeddingBudgetDay.day == call.reserved_day).with_for_update())
            budget.reserved_usd -= call.reserved_usd
            user_budget = _user_budget(db, call.reserved_day, call.owner_user_id)
            if user_budget is not None:
                user_budget.reserved_usd -= call.reserved_usd
        else:
            call.cost_status = "unknown_reserved_ceiling"
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
