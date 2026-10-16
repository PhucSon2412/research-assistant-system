"""PostgreSQL-backed ingest state machine; Redis carries job IDs only.

Beat re-discovers all eligible rows, including M1c jobs and rows left after a
broker outage. Row locks claim a lease. Every result commit fences the token
and rights epoch, so an expired worker cannot publish after a replacement.

Two origins run: ``upload`` (a private item's own file, ``SourceFile.job_id``)
and ``promotion`` (M6b-2b2b: a published reviewed common copy, the file pinned
in ``IngestionJob.source_file_id``; the common file's ``job_id`` stays NULL).
Extraction stops at awaiting_index; index has its own C2d-5d2b boundary.
A promotion step re-checks the common rights gate (``app.common_index``) with
the item locked, so a revoke either committed before (the attempt stops
without further content I/O or writes) or waits for the checkpoint commit. Every step locks the **item
first, then the job**, the order revoke, publish and cleanup use, so a revoke
that holds the item and then flags the job cannot deadlock with a worker.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from app import auth_safety
from app.ingest import checkpoints, runtime
from app.citations import reconcile_references
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.ingest.markdown import PIPELINE_VERSION, chunk_hash, chunk_section, extract, has_body_text
from app.ingest import pdf
from app.models import Chunk, IngestionJob, LibraryItem, MetadataCandidate, PaperVersion, PdfPage, ReferenceEntry, Section, SourceFile
from app.models.enums import JobOrigin, JobState, VerifiedMime, Visibility
from app.storage import OriginalStorage

STEPS = {"validate": JobState.VALIDATING.value, "extract": JobState.EXTRACTING.value,
         "chunk": JobState.CHUNKING.value, "index": JobState.INDEXING.value}
TERMINAL = {JobState.DUPLICATE.value, JobState.TERMINAL_FAILED.value,
         JobState.FULLTEXT_REJECTED.value, JobState.NEEDS_REVIEW.value, JobState.CANCELLED.value,
         JobState.READY.value}
WORKER_STOP = TERMINAL | {JobState.AWAITING_INDEX.value}
CLAIMABLE = {JobState.RECEIVED.value, JobState.VALIDATING.value, JobState.STORED.value,
             JobState.EXTRACTING.value, JobState.CHUNKING.value, JobState.RETRYABLE_FAILED.value}


class StepError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _job_files(db: Session, job: IngestionJob) -> list[SourceFile]:
    if job.origin == JobOrigin.PROMOTION.value:
        source = db.get(SourceFile, job.source_file_id) if job.source_file_id else None
        return [source] if source is not None else []
    return list(db.scalars(select(SourceFile).where(SourceFile.job_id == job.id).execution_options(populate_existing=True)).all())


def _context(db: Session, job: IngestionJob, item: LibraryItem | None) -> tuple[LibraryItem, PaperVersion, SourceFile]:
    """The job's item (already locked by the caller), version and file, or the reason it may not run."""
    version = db.scalar(select(PaperVersion).where(PaperVersion.id == job.version_id)
        .execution_options(populate_existing=True)) if job.version_id else None
    files = _job_files(db, job)
    if (item is None or version is None or len(files) != 1 or version.item_id != item.id
            or files[0].item_id != item.id or files[0].version_id != version.id):
        raise StepError("invalid_job_file_relation")
    if job.origin == JobOrigin.PROMOTION.value:
        if job.scope_visibility != Visibility.COMMON.value or item.visibility != Visibility.COMMON.value:
            raise StepError("rights_gate_missing")
        _guard(job, item)  # a revoke bumps the epoch: the job is cancelled, not refused
        from app.common_index import block_reason
        reason, _, source = block_reason(db, item)
        if reason == "revoked":
            raise StepError("cancelled")
        if reason is not None:
            raise StepError(f"common_{reason}")
        if source.id != files[0].id:
            raise StepError("common_file_mismatch")
    elif (job.origin != JobOrigin.UPLOAD.value or job.scope_visibility != Visibility.PRIVATE.value
            or item.visibility != Visibility.PRIVATE.value or job.scope_owner_user_id != item.owner_user_id):
        raise StepError("rights_gate_missing")
    if files[0].verified_mime not in (VerifiedMime.MARKDOWN.value, VerifiedMime.PDF.value):
        raise StepError("unsupported_file_kind")
    return item, version, files[0]


def _guard(job: IngestionJob, item: LibraryItem) -> None:
    if item.deleted_at is not None or job.cancel_requested or item.rights_epoch != job.rights_epoch_at_start:
        raise StepError("cancelled")


def extraction_state(job) -> bool:
    return job.state in CLAIMABLE and (job.state != 'retryable_failed'
        or job.resume_state in (None, 'validating', 'extracting', 'chunking', 'stored'))


def stage(state):
    return 'validate' if state == 'validating' else 'extract' if state in ('stored', 'extracting') else 'chunk'


_lock_item_then_job = checkpoints.lock_item_then_job
claim_job = checkpoints.claim_job
_locked = checkpoints.leased
_finish = checkpoints.finish
_fail = checkpoints.fail


def _begin(db, job_id, token, step):
    proof = checkpoints.begin(db, job_id, token, step)
    db.info['worker_source_proof'] = proof
    return proof


def _read_markdown(source: SourceFile, storage: OriginalStorage, settings: Settings) -> str:
    if source.byte_size > settings.files.markdown_max_bytes:
        raise StepError("file_too_large")
    try:
        body = storage.open(source.storage_key)
        try:
            raw = body.read(settings.files.markdown_max_bytes + 1)
        finally:
            body.close()
    except Exception:
        raise StepError("storage_unavailable", retryable=True) from None
    if (len(raw) != source.byte_size or len(raw) > settings.files.markdown_max_bytes
            or hashlib.sha256(raw).hexdigest() != source.checksum_sha256):
        raise StepError("checksum_mismatch")
    try:
        original = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise StepError("invalid_markdown") from None
    if not original.strip() or "\x00" in original:
        raise StepError("invalid_markdown")
    return original


def _read_pdf(source: SourceFile, storage: OriginalStorage, settings: Settings) -> bytes:
    if source.byte_size > settings.files.pdf_max_bytes or not source.page_count or source.page_count > settings.files.pdf_max_pages:
        raise StepError("pdf_limit_invalid")
    try:
        body = storage.open(source.storage_key)
        try:
            raw = body.read(settings.files.pdf_max_bytes + 1)
        finally:
            body.close()
    except Exception:
        raise StepError("storage_unavailable", retryable=True) from None
    if len(raw) != source.byte_size or hashlib.sha256(raw).hexdigest() != source.checksum_sha256:
        raise StepError("checksum_mismatch")
    if not raw.startswith(b"%PDF-"):
        raise StepError("pdf_invalid")
    return raw


def _validate(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
              storage: OriginalStorage, settings: Settings) -> str:
    checkpoints.io_check(db, job_id, token, source)
    if source.verified_mime == VerifiedMime.PDF.value:
        _read_pdf(source, storage, settings)
    else:
        _read_markdown(source, storage, settings)

    def save(db: Session, job: IngestionJob, item: LibraryItem, _version: PaperVersion, source: SourceFile) -> str:
        if job.origin == JobOrigin.PROMOTION.value:
            # A common copy is one reviewed file; exact-byte duplicates are the publication's rule.
            return JobState.STORED.value
        # Transaction advisory lock serializes same-owner/same-hash validation.
        # It avoids both jobs choosing the other as canonical.
        lock_key = f"{item.owner_user_id}:{source.checksum_sha256}"
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key})
        other = db.scalar(select(SourceFile.id).join(IngestionJob, SourceFile.job_id == IngestionJob.id)
            .join(LibraryItem, LibraryItem.id == SourceFile.item_id)
            .where(SourceFile.checksum_sha256 == source.checksum_sha256,
                   LibraryItem.deleted_at.is_(None),
                   SourceFile.id != source.id, IngestionJob.scope_visibility == Visibility.PRIVATE.value,
                   IngestionJob.scope_owner_user_id == item.owner_user_id,
                   IngestionJob.state.in_([JobState.STORED.value, JobState.EXTRACTING.value,
                                           JobState.CHUNKING.value, JobState.AWAITING_INDEX.value]))
            .order_by(IngestionJob.created_at, IngestionJob.id).limit(1))
        if other is not None:
            job.warnings = ["duplicate_in_private_library"]
            return JobState.DUPLICATE.value
        return JobState.STORED.value

    return _finish(db, job_id, token, "validate", save)


def _rows(db: Session, model, source: SourceFile, pipeline: str, order) -> list:
    order = order if isinstance(order, tuple) else (order,)
    return list(db.scalars(select(model).where(model.source_file_id == source.id, model.pipeline_version == pipeline)
                           .order_by(*order)).all())


def _reuse(existing: list, fresh: list[tuple], key) -> bool:
    """True when rows of this file/pipeline already exist and equal the fresh result (a later
    job of the same common file); ``extraction_changed`` when they differ; False when there are
    none. Same bytes (checked) and pipeline version give the same rows, so none is written twice."""
    if not existing:
        return False
    if [key(row) for row in existing] != fresh:
        raise StepError("extraction_changed")
    return True


def _extract(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
             storage: OriginalStorage, settings: Settings) -> str:
    if source.verified_mime == VerifiedMime.PDF.value:
        return _extract_pdf(db, job_id, token, source, storage, settings)
    checkpoints.io_check(db, job_id, token, source)
    original = _read_markdown(source, storage, settings)
    checkpoints.io_check(db, job_id, token, source)
    result = extract(original)
    body_present = has_body_text(result)

    def save(db: Session, job: IngestionJob, item: LibraryItem, version: PaperVersion, source: SourceFile) -> str:
        references = [ref for section in result.sections if section.kind == "references"
                      for ref in pdf._references(0, original, section.start_offset, section.end_offset)]
        if not _reuse(_rows(db, MetadataCandidate, source, PIPELINE_VERSION, MetadataCandidate.field_name),
                      sorted((c.field_name, c.value, c.start_offset, c.end_offset) for c in result.candidates),
                      lambda r: (r.field_name, r.value, r.start_offset, r.end_offset)):
            for candidate in result.candidates:
                db.add(MetadataCandidate(item_id=item.id, version_id=version.id, source_file_id=source.id,
                       field_name=candidate.field_name, value=candidate.value,
                       locator_kind="markdown",
                       start_offset=candidate.start_offset, end_offset=candidate.end_offset,
                       pipeline_version=PIPELINE_VERSION))
        if not _reuse(_rows(db, Section, source, PIPELINE_VERSION, Section.ordinal),
                      [(s.kind, s.ordinal, s.text, s.heading, s.start_offset, s.end_offset, s.start_line, s.end_line) for s in result.sections],
                      lambda r: (r.kind, r.ordinal, r.text, r.heading, r.start_offset, r.end_offset, r.start_line, r.end_line)):
            for section in result.sections:
                db.add(Section(item_id=item.id, version_id=version.id, source_file_id=source.id,
                       kind=section.kind, heading=section.heading, ordinal=section.ordinal, text=section.text,
                       locator_kind="markdown",
                       start_offset=section.start_offset, end_offset=section.end_offset,
                       start_line=section.start_line, end_line=section.end_line, pipeline_version=PIPELINE_VERSION))
        if not _reuse(_rows(db, ReferenceEntry, source, PIPELINE_VERSION, ReferenceEntry.ordinal),
                      [(n, ref.raw, ref.start, ref.end, ref.fields, ref.confidence) for n, ref in enumerate(references)],
                      lambda r: (r.ordinal, r.raw_citation, r.start_offset, r.end_offset, r.fields, r.confidence)):
            for ref_ordinal, ref in enumerate(references):
                db.add(ReferenceEntry(item_id=item.id, version_id=version.id, source_file_id=source.id,
                    ordinal=ref_ordinal, raw_citation=ref.raw, fields=ref.fields, confidence=ref.confidence,
                    provenance=PIPELINE_VERSION, locator_kind="markdown", page_number=None,
                    start_offset=ref.start, end_offset=ref.end, pipeline_version=PIPELINE_VERSION))
        db.flush()
        reconcile_references(db, source_file_id=source.id, pipeline_version=PIPELINE_VERSION)
        uncertain = "bibliography_boundary_uncertain" in result.warnings
        version.coverage = result.coverage if body_present and not uncertain else "none"
        job.warnings = result.warnings if body_present else result.warnings + ["no_body_text"]
        if uncertain:
            job.last_error_code = "bibliography_boundary_uncertain"
            return JobState.NEEDS_REVIEW.value
        if not body_present:
            job.last_error_code = "no_body_text"
        return JobState.CHUNKING.value if body_present else JobState.FULLTEXT_REJECTED.value

    return _finish(db, job_id, token, "extract", save)


def _extract_pdf(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
                 storage: OriginalStorage, settings: Settings) -> str:
    checkpoints.io_check(db, job_id, token, source)
    raw = _read_pdf(source, storage, settings)
    checkpoints.io_check(db, job_id, token, source)
    try:
        result = pdf.classify(pdf.parse(raw, source.page_count, settings), settings)
    except pdf.PdfError as exc:
        raise StepError(exc.code, exc.retryable) from None

    def save(db: Session, job: IngestionJob, item: LibraryItem, version: PaperVersion, source: SourceFile) -> str:
        body_chars = {n: 0 for n in result.body_pages}
        for segment in result.segments:
            if segment.kind == "body":
                body_chars[segment.page] += sum(not c.isspace() for c in segment.text)
        reuse_pages = _reuse(_rows(db, PdfPage, source, pdf.PIPELINE_VERSION, PdfPage.page_number),
                             [(page.number, page.text, page.width, page.height, page.layout_mode) for page in result.pages],
                             lambda r: (r.page_number, r.text, r.width, r.height, r.layout_mode))
        reuse_sections = _reuse(_rows(db, Section, source, pdf.PIPELINE_VERSION, Section.ordinal),
                                [(s.kind, n, s.text, s.page, s.start, s.end) for n, s in enumerate(result.segments) if s.text],
                                lambda r: (r.kind, r.ordinal, r.text, r.start_page,
                                           (r.locator_detail or {}).get("start_offset"), (r.locator_detail or {}).get("end_offset")))
        reuse_references = _reuse(_rows(db, ReferenceEntry, source, pdf.PIPELINE_VERSION, ReferenceEntry.ordinal),
                                  [(n, ref.raw, ref.page, ref.start, ref.end, ref.fields, ref.confidence) for n, ref in enumerate(result.references)],
                                  lambda r: (r.ordinal, r.raw_citation, r.page_number, r.start_offset, r.end_offset, r.fields, r.confidence))
        for page in ([] if reuse_pages else result.pages):
            kinds = {s.kind for s in result.segments if s.page == page.number}
            role = ("unextractable" if page.number in result.failed_pages else
                    "bibliography_uncertain" if page.number in result.uncertain_reference_pages else
                    "mixed" if len(kinds) > 1 else
                    next(iter(kinds), "cover_or_unknown" if page.text.strip() else "empty"))
            db.add(PdfPage(item_id=item.id, version_id=version.id, source_file_id=source.id,
                page_number=page.number, text=page.text, width=page.width, height=page.height,
                role=role, body_chars=body_chars.get(page.number, 0),
                body_page=page.number in result.body_pages,
                qualifies=page.number in result.qualifying_pages,
                error_code=page.error or ("page_text_missing" if page.number in result.failed_pages else None),
                layout_mode=page.layout_mode,
                pipeline_version=pdf.PIPELINE_VERSION))
        for ordinal, segment in enumerate([] if reuse_sections else result.segments):
            if not segment.text:
                continue
            page = result.pages[segment.page - 1]
            db.add(Section(item_id=item.id, version_id=version.id, source_file_id=source.id,
                kind=segment.kind, heading=None, ordinal=ordinal, text=segment.text,
                locator_kind="pdf", start_page=segment.page, end_page=segment.page,
                locator_detail={"start_offset": segment.start, "end_offset": segment.end,
                                "offset_unit": "page_text_codepoint", "page_width_pt": page.width,
                                "page_height_pt": page.height, "geometry": None,
                                "parser": pdf.PIPELINE_VERSION}, pipeline_version=pdf.PIPELINE_VERSION))
        for ordinal, ref in enumerate([] if reuse_references else result.references):
            db.add(ReferenceEntry(item_id=item.id, version_id=version.id, source_file_id=source.id,
                ordinal=ordinal, raw_citation=ref.raw, fields=ref.fields, confidence=ref.confidence,
                provenance=pdf.PIPELINE_VERSION, locator_kind="pdf", page_number=ref.page,
                start_offset=ref.start, end_offset=ref.end, pipeline_version=pdf.PIPELINE_VERSION))
        db.flush()
        reconcile_references(db, source_file_id=source.id, pipeline_version=pdf.PIPELINE_VERSION)
        version.coverage = result.coverage
        job.warnings = result.warnings
        if result.disposition != JobState.AWAITING_INDEX.value:
            job.last_error_code = "pdf_body_uncertain" if result.uncertain else "pdf_body_below_threshold"
        return JobState.CHUNKING.value if result.disposition == JobState.AWAITING_INDEX.value else result.disposition

    return _finish(db, job_id, token, "extract", save)


def _chunk(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
           storage: OriginalStorage, settings: Settings) -> str:
    if source.verified_mime == VerifiedMime.PDF.value:
        return _chunk_pdf(db, job_id, token, source, storage, settings)
    checkpoints.io_check(db, job_id, token, source)
    original = _read_markdown(source, storage, settings)
    checkpoints.io_check(db, job_id, token, source)
    sections = source.sections
    if not sections:
        raise StepError("missing_sections")
    chunks = []
    for section in sections:
        if original[section.start_offset:section.end_offset] != section.text:
            raise StepError("locator_mismatch")
        for span in chunk_section(original, section, settings.ingest.markdown_max_chunk_chars):
            chunks.append((section, span))
    if not chunks:
        raise StepError("no_extractable_text")

    def save(db: Session, _job: IngestionJob, _item: LibraryItem, _version: PaperVersion,
             source: SourceFile) -> str:
        if tuple(checkpoints.frozen(s) for s in _rows(db, Section, source, PIPELINE_VERSION, Section.ordinal)) != sections:
            raise runtime.Halt('worker_binding_changed')
        if _reuse(_rows(db, Chunk, source, PIPELINE_VERSION, (Chunk.section_id, Chunk.ordinal)),
                  sorted((str(section.id), span.ordinal, span.text, span.start_offset, span.end_offset, chunk_hash(span.text)) for section, span in chunks),
                  lambda r: (str(r.section_id), r.ordinal, r.text, r.start_offset, r.end_offset, r.chunk_hash)):
            return JobState.AWAITING_INDEX.value
        for section, span in chunks:
            db.add(Chunk(section_id=section.id, item_id=section.item_id, version_id=section.version_id,
                         source_file_id=section.source_file_id, ordinal=span.ordinal, text=span.text,
                         locator_kind="markdown",
                         start_offset=span.start_offset, end_offset=span.end_offset,
                         start_line=span.start_line, end_line=span.end_line,
                         chunk_hash=chunk_hash(span.text), pipeline_version=PIPELINE_VERSION))
        return JobState.AWAITING_INDEX.value

    return _finish(db, job_id, token, "chunk", save)


def _chunk_pdf(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
               storage: OriginalStorage, settings: Settings) -> str:
    checkpoints.io_check(db, job_id, token, source)
    _read_pdf(source, storage, settings)
    checkpoints.io_check(db, job_id, token, source)
    pages = {p.page_number: p for p in source.pages}
    sections = source.sections
    if not sections:
        raise StepError("missing_sections")
    chunks = []
    for section in sections:
        page = pages.get(section.start_page)
        detail = section.locator_detail or {}
        start, end = detail.get("start_offset"), detail.get("end_offset")
        if page is None or start is None or end is None or page.text[start:end] != section.text:
            raise StepError("locator_mismatch")
        span_section = SimpleNamespace(kind=section.kind, heading=None, start_offset=start, end_offset=end)
        for span in chunk_section(page.text, span_section, settings.ingest.markdown_max_chunk_chars):
            chunks.append((section, span, page))
    if not chunks:
        raise StepError("no_extractable_text")

    def save(db: Session, _job: IngestionJob, _item: LibraryItem, _version: PaperVersion,
             source: SourceFile) -> str:
        if (tuple(checkpoints.frozen(s) for s in _rows(db, Section, source, pdf.PIPELINE_VERSION, Section.ordinal)) != sections
                or {p.page_number: checkpoints.frozen(p) for p in _rows(db, PdfPage, source, pdf.PIPELINE_VERSION, PdfPage.page_number)} != pages):
            raise runtime.Halt('worker_binding_changed')
        if _reuse(_rows(db, Chunk, source, pdf.PIPELINE_VERSION, (Chunk.section_id, Chunk.ordinal)),
                  sorted((str(section.id), span.ordinal, span.text, page.page_number, span.start_offset, span.end_offset, chunk_hash(span.text)) for section, span, page in chunks),
                  lambda r: (str(r.section_id), r.ordinal, r.text, r.start_page,
                             (r.locator_detail or {}).get("start_offset"), (r.locator_detail or {}).get("end_offset"), r.chunk_hash)):
            return JobState.AWAITING_INDEX.value
        for section, span, page in chunks:
            db.add(Chunk(section_id=section.id, item_id=section.item_id, version_id=section.version_id,
                source_file_id=section.source_file_id, ordinal=span.ordinal, text=span.text,
                locator_kind="pdf", start_page=page.page_number, end_page=page.page_number,
                locator_detail={"start_offset": span.start_offset, "end_offset": span.end_offset,
                                "offset_unit": "page_text_codepoint", "page_width_pt": page.width,
                                "page_height_pt": page.height, "geometry": None,
                                "parser": pdf.PIPELINE_VERSION},
                chunk_hash=chunk_hash(span.text), pipeline_version=pdf.PIPELINE_VERSION))
        return JobState.AWAITING_INDEX.value

    return _finish(db, job_id, token, "chunk", save)


def process_job(job_id: uuid.UUID, settings: Settings | None = None,
                storage: OriginalStorage | None = None) -> str:
    try:
        settings = settings or get_settings()
        if settings.db is None:
            return 'worker_runtime_configuration_required'
        db = get_sessionmaker()()
    except Exception:
        return 'worker_runtime_configuration_required'
    try:
        checkpoints.entry(db)
        def preflight(_):
            current = db.get(IngestionJob, job_id)
            return current is not None and (current.state in ('awaiting_index', 'indexing')
                or current.state == 'retryable_failed' and current.resume_state == 'indexing')
        if runtime.short(db, preflight):
            from app.ingest import document_index, document_accounting
            document_index.configured(settings)
            document_accounting.reap(db, settings)
            token = document_index.claim(db, job_id, settings)
            if token is None:
                return 'not_claimed'
            return document_index.process_index(db, job_id, token,
                storage or OriginalStorage(settings), settings)
        token = claim_job(db, job_id, settings)
        if token is None:
            return 'not_claimed'
        storage = storage or OriginalStorage(settings)
        state = 'validating'
        # Read durable state only through a NEW gated transaction, after known
        # claim; never infer whether an uncertain commit landed from a SELECT.
        def read(_):
            job = db.get(IngestionJob, job_id)
            return job.state if job is not None else 'cancelled'
        state = runtime.short(db, read)
        while state not in WORKER_STOP:
            step = stage(state)
            try:
                source = _begin(db, job_id, token, step)
                operation = {'validate': _validate, 'extract': _extract, 'chunk': _chunk}[step]
                state = operation(db, job_id, token, source, storage, settings)
            except runtime.Halt:
                raise
            except StepError as exc:
                if exc.code in ('cancelled', 'lease_lost', 'invalid_job_file_relation', 'rights_gate_missing') or exc.code.startswith('common_'):
                    return exc.code
                _fail(db, job_id, token, step, exc, settings)
                return exc.code
            except Exception:
                # SQL/teardown/commit failures are never treated as parser errors.
                if db.in_transaction():
                    raise runtime.Halt('worker_checkpoint_unavailable') from None
                _fail(db, job_id, token, step, StepError('processing_unavailable', retryable=True), settings)
                return 'processing_unavailable'
        return state
    except runtime.Halt as exc:
        return exc.code
    except StepError as exc:
        return exc.code
    except Exception:
        return 'worker_checkpoint_unavailable'
    finally:
        try:
            runtime.release(db)
        except runtime.Halt:
            pass


def extraction_due_job_ids(db: Session, settings: Settings, now: datetime | None = None) -> list[uuid.UUID]:
    # Candidate lookup is not claim proof. Clock argument is legacy compatibility;
    # actual eligibility uses a fresh clock after SQL and final journal wait.
    checkpoints.entry(db)
    def read(_):
        candidates = list(db.scalars(select(IngestionJob.id).where(
            IngestionJob.state.in_(CLAIMABLE),
            IngestionJob.finished_at.is_(None),
            or_(IngestionJob.state != 'retryable_failed', IngestionJob.resume_state.is_(None),
                IngestionJob.resume_state.in_(['validating', 'stored', 'extracting', 'chunking']))
        ).order_by(IngestionJob.created_at, IngestionJob.id)))
        db.rollback()
        result = []
        for job_id in candidates:
            job, item = checkpoints.lock_item_then_job(db, job_id, skip=True)
            if job is None:
                db.rollback()
                continue
            try:
                checkpoints.context(db, job, item)
            except (StepError, runtime.Halt):
                db.rollback()
                continue
            auth_safety.require_final(db)
            checkpoints.context(db, job, item)
            at = utcnow()
            if (extraction_state(job) and (job.lease_until is None or job.lease_until <= at)
                    and (job.next_attempt_at is None or job.next_attempt_at <= at)):
                result.append(job_id)
            # No resource locks after journal: each candidate has its own transaction.
            db.rollback()
            if len(result) >= settings.ingest.dispatch_batch:
                break
        return result
    return runtime.short(db, read)


def due_job_ids(db: Session, settings: Settings, now: datetime | None = None) -> list[uuid.UUID]:
    extracted = extraction_due_job_ids(db, settings, now)
    from app.ingest import document_index, document_accounting
    document_accounting.reap(db, settings)
    try:
        document_index.configured(settings)
    except runtime.Halt:
        return extracted
    return (extracted + document_index.due(db, settings))[:settings.ingest.dispatch_batch]


def dispatchable(job_id, settings=None):
    settings = settings or get_settings()
    db = get_sessionmaker()()
    try:
        # Fresh proof immediately before broker I/O; connection is released by
        # due_job_ids. Consumer independently proves its own claim.
        return job_id in due_job_ids(db, settings)
    except Exception:
        return False
    finally:
        try:
            runtime.release(db)
        except runtime.Halt:
            pass
