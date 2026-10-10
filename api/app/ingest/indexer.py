"""Build keyword/vector generations before atomically publishing them.

Private uploads (M3a) and, since M6b-2b2b, published reviewed common copies
(``promotion`` jobs) use the same steps. For a common copy the rights gate of
``app.common_index`` replaces the upload acknowledgement and runs, with the
item locked, before every reservation, immediately before each provider call,
after each response and in the publishing transaction.

No storage I/O happens while the item and job are locked (review PR #23): a
revoke never waits on a slow object read. The whole object is read and its
size and SHA-256 checked, with no lock held, before every provider call (the
read that opens a run serves the first call) and, always, once more after the
last index entry is written and immediately before the publishing transaction,
however many calls were new or reused; only then are the job, lease, cancel flag, rights, epoch and tombstone
checked under the lock and committed before the call, so no lock is held while
the provider works either. A HEAD of size/ETag/Last-Modified is used only to
reject early (around each full read and after each response); it never
replaces the hash. Between the last hash and the call there are only DB
steps: an object replaced in that gap is not seen before that call, but the
next hash (before the next call or before publishing) refuses it. A common copy's calls are charged to
the application and embedding buckets only (``owner_user_id`` NULL), never to a
User's quota. Search/RAG retain their existing exact resource gates; provider
runtime for query/rerank/RAG is outside this module's certification.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import COMMON_INDEX_VERSION
from app.config import Settings
from app.ingest import markdown, pdf
from app.ingest.embedding import EmbeddingError, embed
from app.ingest.worker import StepError, _read_markdown, _read_pdf, utcnow
from app.models import (Chunk, EmbeddingCall, EmbeddingUserBudgetDay, IndexEntry, IndexGeneration,
                        IngestionJob, IngestStepAttempt, LibraryItem, PaperVersion, PdfPage, Section, SourceFile)
from app.models.enums import JobOrigin, JobState, VerifiedMime
from app.storage import OriginalStorage, is_missing_object

INDEX_VERSION = "private-keyword-vector-v1"


def index_version(job: IngestionJob) -> str:
    return COMMON_INDEX_VERSION if job.origin == JobOrigin.PROMOTION.value else INDEX_VERSION


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
                or chunk.version_id != version.id or chunk.source_file_id != source.id or section.source_file_id != source.id
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


def process_index(db, job_id, token, storage, settings):
    """Runtime entry only. No legacy transaction/provider path remains."""
    from app.ingest.document_index import process_index as run
    return run(db, job_id, token, storage, settings)


def _insert_entry(db: Session, generation: IndexGeneration, chunk: Chunk, call: EmbeddingCall) -> None:
    if call.status != "completed" or call.dimensions is None:
        raise StepError("embedding_incomplete")
    if generation.dimensions is not None and generation.dimensions != call.dimensions:
        raise StepError("embedding_dimensions_mismatch")
    if (call.vector is None or call.content_removed_at is not None
            or call.source_file_id != generation.source_file_id or call.model_id != generation.model_id
            or call.provider != generation.provider
            or call.pipeline_version != chunk.pipeline_version or call.chunk_hash != chunk.chunk_hash):
        raise StepError('index_vector_mismatch')
    generation.dimensions = call.dimensions
    db.add(IndexEntry(generation_id=generation.id, chunk_id=chunk.id, embedding_call_id=call.id,
                      chunk_hash=chunk.chunk_hash, text=chunk.text))


def _publish(db: Session, job: IngestionJob, item: LibraryItem, generation: IndexGeneration,
             chunks: list[Chunk], settings: Settings) -> None:
    if generation.provider != settings.embedding.provider or generation.model_id != settings.embedding.model_id:
        raise StepError('index_generation_changed')
    entries = db.scalars(select(IndexEntry).where(IndexEntry.generation_id == generation.id)).all()
    if (generation.dimensions is None or len(entries) != generation.expected_chunks
            or {e.chunk_id for e in entries} != {c.id for c in chunks}):
        raise StepError("index_incomplete")
    for entry in entries:
        chunk = next(c for c in chunks if c.id == entry.chunk_id)
        if entry.text != chunk.text or entry.chunk_hash != chunk.chunk_hash:
            raise StepError('index_keyword_mismatch')
        call = db.get(EmbeddingCall, entry.embedding_call_id)
        if (call.status != "completed" or call.vector is None or call.dimensions != generation.dimensions
                or call.model_id != generation.model_id or entry.chunk_hash != call.chunk_hash
                or call.source_file_id != generation.source_file_id or call.pipeline_version != generation.pipeline_version
                or call.provider != generation.provider
                or call.content_removed_at is not None or len(call.vector) != generation.dimensions
                or not all(math.isfinite(float(v)) for v in call.vector)):
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
