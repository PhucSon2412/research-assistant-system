"""Index a published reviewed common copy through the shared pipeline (M6b-2b2b).

The gate (``block_reason``) is the same for the Admin action that starts a
job and for the worker at every step: the item is a ``listed`` reviewed copy
without a tombstone; it has exactly one AccessRights row, live, granted at the
item's current ``rights_epoch``, naming the file/version/checksum of its live
publication (whose verified size is the file's); those rights let
``instance_users`` use the full text (``permits_fulltext_search``) **and** send
it to the provider (``permits_external_processing``), with no
``scope_conditions`` (free text the system cannot enforce, as for download);
and the file is Markdown or PDF. ``permits_original_download`` is never
consulted: a copy that may not be downloaded may still be indexed.

Full text without external processing is refused with its own reason: the
current index needs vectors from OpenRouter, so it cannot complete, and a
keyword-only index is not silently built instead. Without
``permits_fulltext_search`` nothing is extracted at all.

``start`` locks the item ``FOR UPDATE`` (the order revoke, publish and cleanup
use: item first, then the job) and either returns the unfinished promotion job
of the file or inserts a new one; a partial unique index keeps it to one per
file whatever the concurrency. The job pins item, version, file and
``rights_epoch_at_start``; its key, label and audit carry IDs only.

Indexing is not opening: a copy that reaches ``indexed`` is still served by no
chunk, search or RAG route (they keep the private-owner gate). That is the next
step, with its own gate before top-k.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, review_copy_admin_clause
from app.config import Settings
from app.models import (AccessRights, CommonPublication, DeletionTombstone, IndexGeneration, IngestionJob, LibraryItem,
                        SourceFile)
from app.models.enums import (AuditActorType, AuditOutcome, ContentStatus, JobOrigin, JobState, PublicationState,
                              RightsAudience, VerifiedMime, Visibility)

INDEXABLE = "indexable"
# Deterministic extraction outcomes: running the same pipeline on the same bytes gives the same answer.
EXTRACTION_REJECTED = (JobState.FULLTEXT_REJECTED.value, JobState.NEEDS_REVIEW.value)
JOB_LABEL = "admin_index"
NOT_OPEN_NOTE = ("Đã lập chỉ mục, chưa mở tìm kiếm/hỏi đáp: bản chung này có index toàn văn nhưng chưa có route "
                 "tìm kiếm, đoạn trích hay hỏi đáp nào phục vụ nó (bước sau).")
# 409 code for each refusal; "revoked" keeps the code publish/revoke already use.
REFUSAL_CODES = {"revoked": "common_copy_revoked", "not_published": "common_copy_not_published",
                 "rights_not_current": "rights_not_current", "audience_not_supported": "audience_not_supported",
                 "fulltext_not_permitted": "fulltext_not_permitted",
                 "external_processing_not_permitted": "external_processing_not_permitted",
                 "blocked_by_conditions": "blocked_by_conditions", "unsupported_file_kind": "unsupported_file_kind"}


def block_reason(db: Session, item: LibraryItem) -> tuple[str | None, AccessRights | None, SourceFile | None]:
    """Why the copy may not be indexed now (``None`` when it may), with its rights and file.

    Reads fresh rows; callers that act on the answer hold the item ``FOR UPDATE``,
    so a revoke (which locks the item first) is either visible here or waits.
    """
    if item.visibility != Visibility.COMMON.value:
        return "rights_not_current", None, None
    tombstoned = db.scalar(select(DeletionTombstone.id).where(DeletionTombstone.item_id == item.id).limit(1))
    if item.publication_state == PublicationState.REVOKED.value or tombstoned is not None:
        return "revoked", None, None
    if item.publication_state != PublicationState.LISTED.value:
        return "not_published", None, None
    rights = db.scalars(select(AccessRights).where(AccessRights.item_id == item.id).order_by(AccessRights.id)
                        .execution_options(populate_existing=True)).all()
    if len(rights) != 1 or rights[0].revoked_at is not None or rights[0].rights_epoch_at_grant != item.rights_epoch:
        return "rights_not_current", None, None
    (recorded,) = rights
    source = db.scalar(select(SourceFile).where(SourceFile.id == recorded.source_file_id)
                       .execution_options(populate_existing=True))
    publication = db.scalar(select(CommonPublication).where(CommonPublication.item_id == item.id)
                            .execution_options(populate_existing=True))
    if (source is None or source.item_id != item.id or source.version_id != recorded.version_id
            or source.checksum_sha256 != recorded.file_checksum_sha256 or publication is None
            or publication.revoked_at is not None or publication.access_rights_id != recorded.id
            or publication.file_checksum_sha256 != recorded.file_checksum_sha256
            or publication.verified_byte_size != source.byte_size):
        return "rights_not_current", recorded, None
    if recorded.scope_audience != RightsAudience.INSTANCE_USERS.value:
        return "audience_not_supported", recorded, source
    if not recorded.permits_fulltext_search:
        return "fulltext_not_permitted", recorded, source
    if not recorded.permits_external_processing:
        return "external_processing_not_permitted", recorded, source
    if recorded.scope_conditions is not None:
        return "blocked_by_conditions", recorded, source
    if source.verified_mime not in (VerifiedMime.MARKDOWN.value, VerifiedMime.PDF.value):
        return "unsupported_file_kind", recorded, source
    return None, recorded, source


def live_job(db: Session, source_file_id: uuid.UUID) -> IngestionJob | None:
    return db.scalar(select(IngestionJob).where(
        IngestionJob.origin == JobOrigin.PROMOTION.value, IngestionJob.source_file_id == source_file_id,
        IngestionJob.finished_at.is_(None)).execution_options(populate_existing=True))


def latest_job(db: Session, item_id: uuid.UUID) -> IngestionJob | None:
    return db.scalar(select(IngestionJob).where(IngestionJob.item_id == item_id,
                                                IngestionJob.origin == JobOrigin.PROMOTION.value)
                     .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc()).limit(1))


@dataclass(frozen=True)
class Started:
    job: IngestionJob
    created: bool


def _audit(db: Session, admin: Principal, outcome: AuditOutcome, item_id: uuid.UUID, **details: str) -> None:
    record_event(db, action="common_copy.index.start", outcome=outcome, actor_type=AuditActorType.USER,
                 actor_user_id=admin.user_id, target_type="library_item", target_id=str(item_id), details=details)


def _refuse(db: Session, admin: Principal, item_id: uuid.UUID, code: str, reason: str) -> ApiError:
    db.rollback()
    try:
        _audit(db, admin, AuditOutcome.DENIED, item_id, reason_code=reason)
        db.commit()
    except Exception:  # the refusal stands even if its audit row cannot be written
        db.rollback()
    return ApiError(409, code)


def start(db: Session, admin: Principal, item_id: uuid.UUID, now: datetime) -> Started:
    """Start (or return the running) index job of one published reviewed copy."""
    item = db.scalar(select(LibraryItem).where(LibraryItem.id == item_id, review_copy_admin_clause(admin))
                     .with_for_update().execution_options(populate_existing=True))
    if item is None:
        db.rollback()
        raise ApiError(404, "not_found")
    reason, _, source = block_reason(db, item)
    if reason is not None:
        raise _refuse(db, admin, item.id, REFUSAL_CODES[reason], reason)
    running = live_job(db, source.id)
    if running is not None:  # a repeated click, or a request that lost the race to this job
        db.commit()
        return Started(running, False)
    if item.content_status == ContentStatus.INDEXED.value or item.active_index_generation_id is not None:
        raise _refuse(db, admin, item.id, "already_indexed", "already_indexed")
    previous = latest_job(db, item.id)
    if previous is not None and previous.state in EXTRACTION_REJECTED:
        raise _refuse(db, admin, item.id, "extraction_rejected", previous.state)
    seq = db.scalar(select(func.count()).select_from(IngestionJob).where(
        IngestionJob.source_file_id == source.id)) + 1
    job = IngestionJob(origin=JobOrigin.PROMOTION.value, actor_user_id=admin.user_id, source_label=JOB_LABEL,
                       scope_visibility=Visibility.COMMON.value, scope_owner_user_id=None, item_id=item.id,
                       version_id=source.version_id, source_file_id=source.id,
                       idempotency_key=f"promotion:{source.id}:{seq}", rights_epoch_at_start=item.rights_epoch,
                       state=JobState.RECEIVED.value, created_at=now, updated_at=now)
    db.add(job)
    try:
        db.flush()
    except IntegrityError:  # cannot happen under the item lock; the partial unique index is the backstop
        db.rollback()
        running = live_job(db, source.id)
        if running is None:
            raise ApiError(503, "index_start_unavailable") from None
        db.commit()
        return Started(running, False)
    _audit(db, admin, AuditOutcome.SUCCESS, item.id, job_id=str(job.id), source_file_id=str(source.id))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise ApiError(503, "index_start_unavailable") from None
    return Started(job, True)


def provider_ready(settings: Settings) -> bool:
    return settings.embedding.api_key is not None and settings.embedding.pricing_verified


def status(db: Session, item: LibraryItem, settings: Settings) -> dict:
    """Admin view: whether the copy may be indexed, its latest job and generation. IDs and codes only."""
    reason, _, _ = block_reason(db, item)
    job = latest_job(db, item.id)
    generation = (db.get(IndexGeneration, item.active_index_generation_id)
                  if item.active_index_generation_id else None)
    indexed = item.content_status == ContentStatus.INDEXED.value
    return {
        "eligibility": reason or INDEXABLE,
        "content_status": item.content_status,
        # Indexing does not open anything: chunk, search and RAG routes do not serve common copies yet.
        "search_open": False,
        "note": NOT_OPEN_NOTE if indexed else None,
        "provider_ready": provider_ready(settings),
        "active_generation": None if generation is None else {
            "id": str(generation.id), "model_id": generation.model_id, "dimensions": generation.dimensions,
            "index_version": generation.index_version, "expected_chunks": generation.expected_chunks,
            "activated_at": generation.activated_at.isoformat() if generation.activated_at else None},
        "job": None if job is None else {
            "id": str(job.id), "state": job.state, "attempt_count": job.attempt_count,
            "last_error_code": job.last_error_code, "warnings": job.warnings,
            "cancel_requested": job.cancel_requested, "created_at": job.created_at.isoformat(),
            "extraction_finished_at": job.extraction_finished_at.isoformat() if job.extraction_finished_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None},
    }
