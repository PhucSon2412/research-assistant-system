"""PostgreSQL-backed ingest state machine; Redis carries job IDs only.

Beat re-discovers all eligible rows, including M1c jobs and rows left after a
broker outage. Row locks claim a lease. Every result commit fences the token
and rights epoch, so an expired worker cannot publish after a replacement.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.ingest.markdown import PIPELINE_VERSION, chunk_hash, chunk_section, extract, has_body_text
from app.models import Chunk, IngestionJob, IngestStepAttempt, LibraryItem, MetadataCandidate, PaperVersion, Section, SourceFile
from app.models.enums import JobState, VerifiedMime, Visibility
from app.storage import OriginalStorage

STEPS = {"validate": JobState.VALIDATING.value, "extract": JobState.EXTRACTING.value,
         "chunk": JobState.CHUNKING.value}
FINAL = {JobState.DUPLICATE.value, JobState.AWAITING_INDEX.value, JobState.TERMINAL_FAILED.value,
         JobState.FULLTEXT_REJECTED.value, JobState.NEEDS_REVIEW.value, JobState.CANCELLED.value,
         JobState.READY.value}
CLAIMABLE = {JobState.RECEIVED.value, JobState.VALIDATING.value, JobState.STORED.value,
             JobState.EXTRACTING.value, JobState.CHUNKING.value, JobState.RETRYABLE_FAILED.value}


class StepError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _context(db: Session, job: IngestionJob) -> tuple[LibraryItem, PaperVersion, SourceFile]:
    # Hold the item row until this step commits. A concurrent epoch bump then
    # orders before our check or after our result, never between the two.
    item = (db.scalar(select(LibraryItem).where(LibraryItem.id == job.item_id)
                      .with_for_update().execution_options(populate_existing=True))
            if job.item_id else None)
    version = db.get(PaperVersion, job.version_id) if job.version_id else None
    files = db.scalars(select(SourceFile).where(SourceFile.job_id == job.id)).all()
    if (item is None or version is None or len(files) != 1 or version.item_id != item.id
            or files[0].item_id != item.id or files[0].version_id != version.id):
        raise StepError("invalid_job_file_relation")
    if (job.origin != "upload" or job.scope_visibility != Visibility.PRIVATE.value
            or item.visibility != Visibility.PRIVATE.value or job.scope_owner_user_id != item.owner_user_id):
        raise StepError("rights_gate_missing")
    if files[0].verified_mime != VerifiedMime.MARKDOWN.value:
        raise StepError("unsupported_file_kind")
    return item, version, files[0]


def _guard(job: IngestionJob, item: LibraryItem) -> None:
    if job.cancel_requested or item.rights_epoch != job.rights_epoch_at_start:
        raise StepError("cancelled")


def claim_job(db: Session, job_id: uuid.UUID, settings: Settings, now: datetime | None = None) -> uuid.UUID | None:
    now = now or utcnow()
    job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id)
                    .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if job is None or job.state not in CLAIMABLE or (job.lease_until and job.lease_until > now):
        db.rollback()
        return None
    if job.state == JobState.RETRYABLE_FAILED.value and job.next_attempt_at and job.next_attempt_at > now:
        db.rollback()
        return None
    if job.attempt_count >= settings.ingest.max_attempts:
        job.state = JobState.TERMINAL_FAILED.value
        job.last_error_code = "attempts_exhausted"
        job.finished_at = now
        db.commit()
        return None
    db.execute(update(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
                 IngestStepAttempt.outcome == "running").values(outcome="retry", error_code="worker_lost", finished_at=now))
    if job.state == JobState.RETRYABLE_FAILED.value:
        job.state = job.resume_state or JobState.VALIDATING.value
    if job.state == JobState.RECEIVED.value:
        job.state = JobState.VALIDATING.value
    job.attempt_count += 1
    job.started_at = job.started_at or now
    job.lease_token = uuid.uuid4()
    job.lease_until = now + timedelta(seconds=settings.ingest.lease_seconds)
    job.next_attempt_at = None
    job.updated_at = now
    token = job.lease_token
    db.commit()
    return token


def _locked(db: Session, job_id: uuid.UUID, token: uuid.UUID) -> tuple[IngestionJob, LibraryItem, PaperVersion, SourceFile]:
    job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id)
                    .with_for_update().execution_options(populate_existing=True))
    if job is None or job.lease_token != token or job.lease_until is None or job.lease_until <= utcnow():
        raise StepError("lease_lost")
    item, version, source = _context(db, job)
    _guard(job, item)
    return job, item, version, source


def _begin(db: Session, job_id: uuid.UUID, token: uuid.UUID, step: str) -> SourceFile:
    job, _, _, source = _locked(db, job_id, token)
    job.state = STEPS[step]
    job.updated_at = utcnow()
    db.add(IngestStepAttempt(job_id=job.id, step=step, attempt_no=job.attempt_count, outcome="running"))
    db.commit()
    return source


def _finish(db: Session, job_id: uuid.UUID, token: uuid.UUID, step: str,
            save: Callable[[Session, IngestionJob, LibraryItem, PaperVersion, SourceFile], str]) -> str:
    job, item, version, source = _locked(db, job_id, token)
    new_state = save(db, job, item, version, source)
    attempt = db.scalar(select(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
                        IngestStepAttempt.step == step, IngestStepAttempt.attempt_no == job.attempt_count))
    assert attempt is not None
    attempt.outcome = "success"
    attempt.finished_at = utcnow()
    job.state = new_state
    job.last_error_code = None
    job.resume_state = None
    job.updated_at = utcnow()
    if new_state in FINAL:
        job.finished_at = utcnow()
        job.lease_token = None
        job.lease_until = None
    db.commit()
    return new_state


def _fail(db: Session, job_id: uuid.UUID, token: uuid.UUID, step: str, exc: StepError,
          settings: Settings) -> None:
    db.rollback()
    job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id)
                    .with_for_update().execution_options(populate_existing=True))
    if job is None or job.lease_token != token:
        db.rollback()
        return
    item = (db.scalar(select(LibraryItem).where(LibraryItem.id == job.item_id)
                      .with_for_update().execution_options(populate_existing=True))
            if job.item_id else None)
    if item is not None and (job.cancel_requested or item.rights_epoch != job.rights_epoch_at_start):
        exc = StepError("cancelled")
    now = utcnow()
    attempt = db.scalar(select(IngestStepAttempt).where(IngestStepAttempt.job_id == job.id,
                        IngestStepAttempt.step == step, IngestStepAttempt.attempt_no == job.attempt_count))
    if attempt is not None and attempt.outcome == "running":
        attempt.outcome = "cancelled" if exc.code == "cancelled" else "retry" if exc.retryable else "failed"
        attempt.error_code = exc.code
        attempt.finished_at = now
    job.last_error_code = exc.code
    job.resume_state = STEPS[step]
    job.lease_token = None
    job.lease_until = None
    job.updated_at = now
    if exc.code == "cancelled":
        job.state = JobState.CANCELLED.value
        job.finished_at = now
    elif exc.code == "no_body_text":
        job.state = JobState.FULLTEXT_REJECTED.value
        job.finished_at = now
        if job.version_id is not None:
            version = db.get(PaperVersion, job.version_id)
            if version is not None:
                version.coverage = "none"
    elif exc.retryable and job.attempt_count < settings.ingest.max_attempts:
        job.state = JobState.RETRYABLE_FAILED.value
        delay = min(settings.ingest.retry_base_seconds * 2 ** (job.attempt_count - 1),
                    settings.ingest.retry_max_seconds)
        job.next_attempt_at = now + timedelta(seconds=delay)
    else:
        job.state = JobState.TERMINAL_FAILED.value
        job.finished_at = now
    db.commit()


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


def _validate(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
              storage: OriginalStorage, settings: Settings) -> str:
    _read_markdown(source, storage, settings)

    def save(db: Session, job: IngestionJob, item: LibraryItem, _version: PaperVersion, source: SourceFile) -> str:
        # Transaction advisory lock serializes same-owner/same-hash validation.
        # It avoids both jobs choosing the other as canonical.
        lock_key = f"{item.owner_user_id}:{source.checksum_sha256}"
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key})
        other = db.scalar(select(SourceFile.id).join(IngestionJob, SourceFile.job_id == IngestionJob.id)
            .where(SourceFile.checksum_sha256 == source.checksum_sha256,
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


def _extract(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
             storage: OriginalStorage, settings: Settings) -> str:
    original = _read_markdown(source, storage, settings)
    result = extract(original)
    if not has_body_text(result):
        raise StepError("no_body_text")

    def save(db: Session, job: IngestionJob, item: LibraryItem, version: PaperVersion, source: SourceFile) -> str:
        for candidate in result.candidates:
            db.add(MetadataCandidate(item_id=item.id, version_id=version.id, source_file_id=source.id,
                   field_name=candidate.field_name, value=candidate.value,
                   locator_kind="markdown",
                   start_offset=candidate.start_offset, end_offset=candidate.end_offset,
                   pipeline_version=PIPELINE_VERSION))
        for section in result.sections:
            db.add(Section(item_id=item.id, version_id=version.id, source_file_id=source.id,
                   kind=section.kind, heading=section.heading, ordinal=section.ordinal, text=section.text,
                   locator_kind="markdown",
                   start_offset=section.start_offset, end_offset=section.end_offset,
                   start_line=section.start_line, end_line=section.end_line, pipeline_version=PIPELINE_VERSION))
        version.coverage = result.coverage
        job.warnings = result.warnings
        return JobState.CHUNKING.value

    return _finish(db, job_id, token, "extract", save)


def _chunk(db: Session, job_id: uuid.UUID, token: uuid.UUID, source: SourceFile,
           storage: OriginalStorage, settings: Settings) -> str:
    original = _read_markdown(source, storage, settings)
    sections = db.scalars(select(Section).where(Section.source_file_id == source.id,
                          Section.pipeline_version == PIPELINE_VERSION).order_by(Section.ordinal)).all()
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
             _source: SourceFile) -> str:
        for section, span in chunks:
            db.add(Chunk(section_id=section.id, item_id=section.item_id, version_id=section.version_id,
                         source_file_id=section.source_file_id, ordinal=span.ordinal, text=span.text,
                         locator_kind="markdown",
                         start_offset=span.start_offset, end_offset=span.end_offset,
                         start_line=span.start_line, end_line=span.end_line,
                         chunk_hash=chunk_hash(span.text), pipeline_version=PIPELINE_VERSION))
        return JobState.AWAITING_INDEX.value

    return _finish(db, job_id, token, "chunk", save)


def process_job(job_id: uuid.UUID, settings: Settings | None = None,
                storage: OriginalStorage | None = None) -> str:
    settings = settings or get_settings()
    storage = storage or OriginalStorage(settings)
    maker = get_sessionmaker()
    with maker() as db:
        token = claim_job(db, job_id, settings)
        if token is None:
            return "not_claimed"
        while True:
            job = db.get(IngestionJob, job_id)
            state = job.state
            if state in FINAL:
                return state
            step = "validate" if state == JobState.VALIDATING.value else \
                   "extract" if state in (JobState.STORED.value, JobState.EXTRACTING.value) else "chunk"
            try:
                source = _begin(db, job_id, token, step)
                if step == "validate":
                    state = _validate(db, job_id, token, source, storage, settings)
                elif step == "extract":
                    state = _extract(db, job_id, token, source, storage, settings)
                else:
                    state = _chunk(db, job_id, token, source, storage, settings)
            except StepError as exc:
                if exc.code == "lease_lost":
                    db.rollback()
                    return "lease_lost"
                _fail(db, job_id, token, step, exc, settings)
                return exc.code
            except Exception:
                _fail(db, job_id, token, step, StepError("processing_unavailable", retryable=True), settings)
                return "processing_unavailable"


def due_job_ids(db: Session, settings: Settings, now: datetime | None = None) -> list[uuid.UUID]:
    now = now or utcnow()
    return list(db.scalars(select(IngestionJob.id).join(SourceFile, SourceFile.job_id == IngestionJob.id)
        .where(IngestionJob.origin == "upload", IngestionJob.scope_visibility == Visibility.PRIVATE.value,
               SourceFile.verified_mime == VerifiedMime.MARKDOWN.value,
               IngestionJob.state.in_([JobState.RECEIVED.value, JobState.VALIDATING.value,
                                      JobState.STORED.value, JobState.EXTRACTING.value,
                                      JobState.CHUNKING.value, JobState.RETRYABLE_FAILED.value]),
               (IngestionJob.lease_until.is_(None) | (IngestionJob.lease_until <= now)),
               (IngestionJob.next_attempt_at.is_(None) | (IngestionJob.next_attempt_at <= now)))
        .order_by(IngestionJob.created_at, IngestionJob.id).limit(settings.ingest.dispatch_batch)))
