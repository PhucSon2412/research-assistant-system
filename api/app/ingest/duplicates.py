"""M6e exact-byte duplicates inside one owner's private library.

One predicate serves the worker's validation decision and the owner's item
DTO. A target is a navigation hint only: never canonical, never a source of
rows, rights, collections or provider results for another item. It is always
the same owner's live private upload whose job/file/version/item binding and
epoch still hold:

| Target job state | Eligible when |
| --- | --- |
| stored, extracting, chunking, awaiting_index, indexing | not finished, item metadata_only without active generation |
| retryable_failed | resume_state stored/extracting/chunking/indexing, not finished, item metadata_only |
| ready | item indexed; active generation ``active`` for exactly this job/file/version/epoch |
| received, validating, retryable_failed→validating | never (not validated yet) |
| duplicate, cancelled, terminal_failed, fulltext_rejected, needs_review | never |

Deleted items, any private deletion decision or tombstone (purged or not),
cancel requests, epoch drift, foreign owners, common copies and promotion jobs
are excluded. The earliest eligible job (created_at, id) is chosen, so no
duplicate becomes a target and a chain A→B→A cannot form. Callers hold no
target lock: the worker's owner User lock serializes it against a private
deletion of the same owner, and the read boundary re-locks visible items.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Select, and_, exists, or_, select

from app.models import (DeletionTombstone, IndexGeneration, IngestionJob, LibraryItem, PaperVersion,
                        PrivateItemDeletion, SourceFile)
from app.models.enums import ContentStatus, JobOrigin, JobState, Visibility

WARNING = "duplicate_in_private_library"
IN_FLIGHT = (JobState.STORED.value, JobState.EXTRACTING.value, JobState.CHUNKING.value,
             JobState.AWAITING_INDEX.value, JobState.INDEXING.value)
RESUMABLE = (JobState.STORED.value, JobState.EXTRACTING.value, JobState.CHUNKING.value,
             JobState.INDEXING.value)


def targets(owner_user_id: uuid.UUID, checksum: str, *, exclude_file_id: uuid.UUID) -> Select:
    """Eligible same-owner private targets of ``checksum``, stable order, no row locks."""
    unindexed = and_(LibraryItem.content_status == ContentStatus.METADATA_ONLY.value,
                     LibraryItem.active_index_generation_id.is_(None),
                     IngestionJob.finished_at.is_(None))
    ready = and_(
        IngestionJob.state == JobState.READY.value,
        LibraryItem.content_status == ContentStatus.INDEXED.value,
        LibraryItem.active_version_id == SourceFile.version_id,
        exists().where(IndexGeneration.id == LibraryItem.active_index_generation_id,
                       IndexGeneration.state == "active", IndexGeneration.job_id == IngestionJob.id,
                       IndexGeneration.source_file_id == SourceFile.id,
                       IndexGeneration.version_id == SourceFile.version_id,
                       IndexGeneration.item_id == LibraryItem.id,
                       IndexGeneration.rights_epoch == LibraryItem.rights_epoch))
    return (select(LibraryItem.id.label("item_id"), SourceFile.id.label("file_id"),
                   IngestionJob.id.label("job_id"))
        .select_from(SourceFile)
        .join(IngestionJob, IngestionJob.id == SourceFile.job_id)
        .join(LibraryItem, LibraryItem.id == SourceFile.item_id)
        .join(PaperVersion, PaperVersion.id == SourceFile.version_id)
        .where(SourceFile.checksum_sha256 == checksum, SourceFile.id != exclude_file_id,
               PaperVersion.item_id == LibraryItem.id,
               IngestionJob.origin == JobOrigin.UPLOAD.value,
               IngestionJob.scope_visibility == Visibility.PRIVATE.value,
               IngestionJob.scope_owner_user_id == owner_user_id,
               IngestionJob.item_id == LibraryItem.id, IngestionJob.version_id == SourceFile.version_id,
               IngestionJob.cancel_requested.is_(False),
               IngestionJob.rights_epoch_at_start == LibraryItem.rights_epoch,
               LibraryItem.visibility == Visibility.PRIVATE.value,
               LibraryItem.owner_user_id == owner_user_id,
               LibraryItem.deleted_at.is_(None),
               ~exists().where(PrivateItemDeletion.item_id == LibraryItem.id),
               ~exists().where(DeletionTombstone.item_id == LibraryItem.id),
               or_(and_(IngestionJob.state.in_(IN_FLIGHT), unindexed),
                   and_(IngestionJob.state == JobState.RETRYABLE_FAILED.value,
                        IngestionJob.resume_state.in_(RESUMABLE), unindexed),
                   ready))
        .order_by(IngestionJob.created_at, IngestionJob.id))
