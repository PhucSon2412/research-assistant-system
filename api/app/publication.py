"""Publish, revoke and clean up reviewed common copies (M6b-2a).

**Publish** is now exclusively ``app.api.common_publish`` (C2d-5c1):
short runtime proofs, common-original size/SHA-256 outside every DB connection
and lock, frozen tuple rechecked under the acceptance locks, operation-owned
commit outcome and a fresh response proof. Only metadata is listed. This module
keeps system cleanup/replay primitives and a legacy revoke helper for labelled
system/adversary tests. HTTP revoke exclusively uses ``app.api.common_revoke``
(C2d-5c2); it never invokes this legacy helper's replay/commit inference.

**Revoke** (pending or listed) is one transaction whose commit is ``t_rev``:
bump ``rights_epoch``, set ``revoked`` (and, for a copy indexed by
``app.common_index``, back to ``metadata_only`` with no active generation),
revoke the rights and publication, flag the item's unfinished jobs
``cancel_requested`` and write a ``deletion_tombstones`` row. It locks the
item, then rights, publication and jobs; the index worker locks the item
before its job too, so the two cannot deadlock. Last, under the journal lock,
it appends the tombstone's record to the revocation journal file (``fsync``)
and mirrors it in the same transaction (M6c-1, ``app.recovery``); the
failure paths of that step are described there. From that commit every normal
route (library list/count/detail, collections) drops the item in SQL; file,
chunk, search and RAG recheck their own rights gates. Private items and other common copies
are untouched.

**Cleanup** has two entry points (C2d-5e1). Normal Celery/CLI/direct cleanup is
``app.ingest.cleanup``: actual runtime login and gate, a durable per-key
deletion fence, S3 outside every DB transaction and separate finalisation.
``operator_cleanup_revoked`` below is the operator/recovery path that
``app.recovery`` calls while traffic and writers are stopped; it refuses a
runtime login so runtime cannot use it to skip its gate. Both remove the same
rows. The operator path works from unpurged tombstones, each in its own
transaction, and is safe to repeat: it locks the tombstone, then the item; deletes the copy's
derived full-text rows in foreign-key order (active pointer, citation links,
references, index entries, generations, chunks, sections, PDF pages, metadata
candidates) and nulls the vector and chunk hash of its embedding calls while
keeping their cost columns (M6b-2b2a); finishes its unfinished jobs as
``cancelled``; removes collection memberships; deletes the common object
unless a row that is not revoked names the same key; and **removes the copy's
metadata** (``RevokedMetadataField``: title, source URL, file name, the rights'
source text, licence URL, evidence note and conditions) unless a retention
basis recorded at revocation names that field. Having been publishable is
not such a basis. The DB refuses ``purged_at`` while a derived row, embedding
content or a field without basis is left, and refuses derived writes to a
revoked copy from ``t_rev`` on (0024). Only the common copy's rows are
touched, never the private item, even with the same checksum.

Tombstones are never deleted. After a restore, ``app.recovery`` replays the
journal records the restored database lacks; until then its recovery gate
keeps the API and workers from serving or processing anything.

Audit rows carry IDs and codes only: no title, source, URL, evidence,
checksum, bytes or secrets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, review_copy_admin_clause
from app.config import get_settings
from app.models import (AccessRights, Chunk, CollectionItem, CommonPublication, DeletionTombstone, EmbeddingCall,
                        IndexEntry, IndexGeneration, IngestionJob, IngestStepAttempt, LibraryItem, MetadataCandidate,
                        PaperVersion, PdfPage, ReferenceCitationLink, ReferenceEntry, RevocationMetadataRetention,
                        Section, SourceFile)
from app.models.enums import (AuditActorType, AuditOutcome, CleanupObjectOutcome, ContentStatus, JobState,
                              PublicationState, RevocationReason, RevokedMetadataField, TombstoneKind)
from app.storage import OriginalStorage, is_missing_object


@dataclass(frozen=True)
class RetentionBasis:
    """Why one field of a revoked copy may be kept: never the value itself."""

    field: RevokedMetadataField
    policy_source: str
    scope: str
    reason: str


@dataclass(frozen=True)
class Revoked:
    item: LibraryItem
    tombstone: DeletionTombstone
    cancelled_jobs: int


def _audit(db: Session, admin: Principal, action: str, outcome: AuditOutcome, item_id: uuid.UUID,
           **details: str | int | list[str]) -> None:
    record_event(db, action=action, outcome=outcome, actor_type=AuditActorType.USER, actor_user_id=admin.user_id,
                 target_type="library_item", target_id=str(item_id), details=details)


def _refuse(db: Session, admin: Principal, action: str, item_id: uuid.UUID, status: int, code: str, reason: str,
            outcome: AuditOutcome = AuditOutcome.DENIED) -> ApiError:
    db.rollback()
    try:
        _audit(db, admin, action, outcome, item_id, reason_code=reason)
        db.commit()
    except Exception:  # the refusal stands even if its audit row cannot be written
        db.rollback()
    return ApiError(status, code)


def _copy(db: Session, admin: Principal, item_id: uuid.UUID, *, lock: bool = False) -> LibraryItem | None:
    stmt = select(LibraryItem).where(LibraryItem.id == item_id, review_copy_admin_clause(admin))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


def live_duplicate(db: Session, checksum: str, item_id: uuid.UUID) -> uuid.UUID | None:
    """Another reviewed copy with the same bytes that is published now (exact SHA-256 only)."""
    return db.scalar(select(CommonPublication.item_id).where(
        CommonPublication.file_checksum_sha256 == checksum, CommonPublication.revoked_at.is_(None),
        CommonPublication.item_id != item_id))


def _cancel_jobs(db: Session, item_id: uuid.UUID, now: datetime) -> int:
    return db.execute(update(IngestionJob).where(
        IngestionJob.item_id == item_id, IngestionJob.cancel_requested.is_(False),
        IngestionJob.finished_at.is_(None)).values(
        cancel_requested=True, cancel_requested_at=now, updated_at=now)).rowcount


def _revoke_rows(db: Session, item: LibraryItem, rights: list[AccessRights], revoked_by: uuid.UUID,
                 reason: str, at: datetime, epoch: int) -> None:
    """Flip item, rights and publication to revoked; the DB checks it all at commit.

    An indexed copy leaves ``indexed`` in the same statement (M6b-2b2b): from
    ``t_rev`` it names no active generation, and cleanup removes the rows.
    """
    item.rights_epoch = epoch
    item.publication_state = PublicationState.REVOKED.value
    item.active_index_generation_id = None
    item.content_status = ContentStatus.METADATA_ONLY.value
    item.updated_at = at
    db.flush()
    for recorded in rights:
        recorded.revoked_at = at
        recorded.revoked_by_user_id = revoked_by
        recorded.revocation_reason_code = reason
        recorded.rights_epoch_at_revoke = item.rights_epoch
    publication = db.scalar(select(CommonPublication).where(CommonPublication.item_id == item.id)
                            .with_for_update())
    if publication is not None and publication.revoked_at is None:
        publication.revoked_at = max(at, publication.published_at)
    db.flush()


def revoke(db: Session, admin: Principal, item_id: uuid.UUID, reason: RevocationReason, now: datetime,
           cleanup_deadline: timedelta, retain: tuple[RetentionBasis, ...] = (), *,
           journal_path: Path | None = None) -> Revoked:
    """``journal_path`` defaults to the configured revocation journal."""
    from app import recovery

    if journal_path is None:
        journal_path = get_settings().recovery.journal_path
    item = _copy(db, admin, item_id, lock=True)
    if item is None:
        db.rollback()
        raise ApiError(404, "not_found")
    if item.publication_state == PublicationState.REVOKED.value:
        raise _refuse(db, admin, "common_copy.revoke", item.id, 409, "common_copy_revoked", "already_revoked")
    rights = list(db.scalars(select(AccessRights).where(AccessRights.item_id == item.id)
                             .order_by(AccessRights.id).with_for_update()).all())
    prior = item.publication_state
    _revoke_rows(db, item, rights, admin.user_id, reason.value, now, item.rights_epoch + 1)
    cancelled = _cancel_jobs(db, item.id, now)
    first = rights[0]
    source = db.get(SourceFile, first.source_file_id)
    tombstone = DeletionTombstone(
        target_kind=TombstoneKind.COMMON_COPY.value, item_id=item.id, version_id=first.version_id,
        source_file_id=first.source_file_id, storage_key=source.storage_key if source else None,
        access_rights_id=first.id, reason_code=reason.value, prior_publication_state=prior,
        rights_epoch=item.rights_epoch, recorded_at=now, recorded_by_user_id=admin.user_id,
        purge_due_at=now + cleanup_deadline)
    db.add(tombstone)
    db.flush()
    bases = [RevocationMetadataRetention(
        tombstone_id=tombstone.id, item_id=item.id, field=basis.field.value, policy_source=basis.policy_source,
        scope=basis.scope, reason=basis.reason, confirmed_by_user_id=admin.user_id, recorded_at=now)
        for basis in retain]
    db.add_all(bases)
    db.flush()
    _audit(db, admin, "common_copy.revoke", AuditOutcome.SUCCESS, item.id, reason_code=reason.value,
           from_state=prior, tombstone_id=str(tombstone.id), cancelled_jobs=cancelled,
           retained_fields=sorted(b.field.value for b in retain))
    try:
        # Journal lock after every row lock; the line is fsynced before the commit below.
        journaled = recovery.record_revocation(db, journal_path, tombstone, bases)
    except recovery.RecoveryError:
        db.rollback()
        raise ApiError(503, "service_recovering") from None
    except Exception:
        db.rollback()
        raise ApiError(503, "revocation_unavailable") from None
    tombstone_id = tombstone.id
    try:
        db.commit()  # t_rev
    except Exception:
        db.rollback()
        if not journaled.written:  # neither the journal nor the database took it
            raise ApiError(503, "revocation_unavailable") from None
        # The journal line is durable, so the revocation must happen: replay it now (a no-op if the
        # commit did land). If that fails too, the gate stays closed until `recover` replays it.
        try:
            _, cancelled = recovery.replay_record(db.get_bind(), journaled.record, now)
        except Exception:
            raise ApiError(503, "revocation_pending_recovery") from None
        item = db.get(LibraryItem, item_id, populate_existing=True)
        tombstone = db.get(DeletionTombstone, tombstone_id)
        if item is None or tombstone is None or item.publication_state != PublicationState.REVOKED.value:
            raise ApiError(503, "revocation_pending_recovery")
    if not journaled.written:
        # Committed, so the copy is closed from t_rev, but the journal lacks it: the database is now
        # ahead of the journal and the gate stays closed until the journal is repaired.
        raise ApiError(503, "revocation_journal_unavailable")
    return Revoked(item, tombstone, cancelled)


# --- cleanup ------------------------------------------------------------------

class CleanupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _gone(db: Session, stmt) -> int:
    return db.execute(stmt.execution_options(synchronize_session=False)).rowcount


def _derived_shared(db: Session, item_id: uuid.UUID) -> bool:
    """An entry of another item's generation names this copy's chunk or embedding call.

    That cannot come from the pipeline; removing it would break that other
    index, so cleanup stops (``derived_rows_shared``) and leaves it to an operator.
    """
    files = select(SourceFile.id).where(SourceFile.item_id == item_id)
    generations = select(IndexGeneration.id).where(IndexGeneration.item_id == item_id)
    calls = select(EmbeddingCall.id).where(EmbeddingCall.source_file_id.in_(files))
    return bool(db.scalar(select(func.count()).select_from(IndexEntry).where(
        IndexEntry.generation_id.not_in(generations),
        or_(IndexEntry.chunk_id.in_(select(Chunk.id).where(Chunk.item_id == item_id)),
            IndexEntry.embedding_call_id.in_(calls)))))


def _remove_derived(db: Session, item_id: uuid.UUID, now: datetime) -> tuple[dict[str, int], int]:
    """Delete the copy's full-text derivatives in foreign-key order and scrub its embedding ledger.

    Every row removed names this item, directly or through its generation,
    reference or source file; a private copy with the same checksum has its own
    rows and files and is not touched. Embedding calls keep model, provider,
    tokens, cost, status and day, and lose the vector and chunk hash. Returns
    counts per table and the number of calls scrubbed. The item is locked.
    """
    files = select(SourceFile.id).where(SourceFile.item_id == item_id)
    generations = select(IndexGeneration.id).where(IndexGeneration.item_id == item_id)
    if _derived_shared(db, item_id):
        raise CleanupError("derived_rows_shared")
    db.execute(update(LibraryItem).where(
        LibraryItem.id == item_id, or_(LibraryItem.active_index_generation_id.is_not(None),
                                       LibraryItem.content_status != ContentStatus.METADATA_ONLY.value))
        .values(active_index_generation_id=None, content_status=ContentStatus.METADATA_ONLY.value))
    removed = {
        "reference_citation_links": _gone(db, delete(ReferenceCitationLink).where(
            ReferenceCitationLink.reference_id.in_(select(ReferenceEntry.id).where(ReferenceEntry.item_id == item_id)))),
        "reference_entries": _gone(db, delete(ReferenceEntry).where(ReferenceEntry.item_id == item_id)),
        "index_entries": _gone(db, delete(IndexEntry).where(IndexEntry.generation_id.in_(generations))),
    }
    scrubbed = _gone(db, update(EmbeddingCall).where(
        EmbeddingCall.source_file_id.in_(files), EmbeddingCall.content_removed_at.is_(None))
        .values(vector=None, chunk_hash=None, content_removed_at=now))
    for model in (IndexGeneration, Chunk, Section, PdfPage, MetadataCandidate):
        removed[model.__tablename__] = _gone(db, delete(model).where(model.item_id == item_id))
    return removed, scrubbed


def _finish_jobs(db: Session, item_id: uuid.UUID, now: datetime) -> int:
    """End the copy's unfinished jobs: a worker still holding a lease loses it, beat never reclaims them."""
    jobs = select(IngestionJob.id).where(IngestionJob.item_id == item_id, IngestionJob.finished_at.is_(None))
    _gone(db, update(IngestStepAttempt).where(IngestStepAttempt.job_id.in_(jobs),
                                              IngestStepAttempt.outcome == "running")
          .values(outcome="cancelled", error_code="cancelled", finished_at=now))
    return _gone(db, update(IngestionJob).where(IngestionJob.id.in_(jobs)).values(
        state=JobState.CANCELLED.value, finished_at=now, updated_at=now, last_error_code="cancelled",
        cancel_requested=True, cancel_requested_at=func.coalesce(IngestionJob.cancel_requested_at, now),
        lease_token=None, lease_until=None, next_attempt_at=None))


def _object_in_use(db: Session, key: str, source_file_id: uuid.UUID | None) -> bool:
    """Whether a row that is not a revoked copy (a private file, a live copy) names the key."""
    return db.execute(text("""SELECT 1 FROM source_files f LEFT JOIN library_items i ON i.id = f.item_id
        WHERE f.storage_key = :k AND f.id IS DISTINCT FROM CAST(:f AS uuid)
          AND (i.id IS NULL OR (i.publication_state IS DISTINCT FROM 'revoked' AND i.deleted_at IS NULL)) LIMIT 1"""),
        {"k": key, "f": source_file_id}).first() is not None


def _remove_object(storage: OriginalStorage, key: str) -> CleanupObjectOutcome:
    try:
        storage.size(key)
    except Exception as exc:
        if is_missing_object(exc):
            return CleanupObjectOutcome.ALREADY_ABSENT
        raise CleanupError("storage_unavailable") from None
    try:
        storage.delete(key)
        storage.size(key)
    except Exception as exc:
        if is_missing_object(exc):
            return CleanupObjectOutcome.DELETED
        raise CleanupError("storage_unavailable") from None
    raise CleanupError("object_still_present")


def _remove_metadata(db: Session, item_id: uuid.UUID, now: datetime) -> tuple[list[str], list[str]]:
    """Null every RevokedMetadataField of the copy that no retention basis names.

    Only rows of the revoked common item are updated; the private item, its
    version/file and the review request are separate rows and stay as they are.
    """
    kept = set(db.scalars(select(RevocationMetadataRetention.field)
                          .where(RevocationMetadataRetention.item_id == item_id)).all())
    removed = [f.value for f in RevokedMetadataField if f.value not in kept]
    F = RevokedMetadataField
    if F.TITLE.value in removed:
        db.execute(update(LibraryItem).where(LibraryItem.id == item_id).values(title=None))
    if F.SOURCE_URL.value in removed:
        db.execute(update(PaperVersion).where(PaperVersion.item_id == item_id).values(
            source_url=None, source_identifier=None, version_label=None))
    if F.ORIGINAL_FILENAME.value in removed:
        db.execute(update(SourceFile).where(SourceFile.item_id == item_id).values(original_filename=None))
    columns = {F.SOURCE_URL.value: "verified_source_url", F.VERIFIED_SOURCE.value: "verified_source",
               F.LICENSE_URL.value: "license_url", F.EVIDENCE_NOTE.value: "evidence_note",
               F.SCOPE_CONDITIONS.value: "scope_conditions"}
    values = {column: None for field, column in columns.items() if field in removed}
    db.execute(update(AccessRights).where(AccessRights.item_id == item_id,
                                          AccessRights.metadata_scrubbed_at.is_(None))
               .values(metadata_scrubbed_at=now, **values))
    return removed, sorted(kept)


def _purge_one(db: Session, storage: OriginalStorage, tombstone_id: uuid.UUID, now: datetime) -> str | None:
    tomb = db.scalar(select(DeletionTombstone).where(DeletionTombstone.id == tombstone_id)
                     .with_for_update(skip_locked=True))
    if tomb is None or tomb.purged_at is not None:
        db.rollback()
        return None
    # Tombstone, then item: revoke and replay lock the item and insert (never lock) a tombstone.
    item = db.scalar(select(LibraryItem).where(LibraryItem.id == tomb.item_id).with_for_update()
                     .execution_options(populate_existing=True))
    if item is not None and item.publication_state != PublicationState.REVOKED.value:
        raise CleanupError("target_not_revoked")
    # From t_rev the DB refuses new derived rows (0024), so every row to remove is committed and visible.
    derived, scrubbed = _remove_derived(db, tomb.item_id, now) if item is not None else ({}, 0)
    jobs = _finish_jobs(db, tomb.item_id, now) if item is not None else 0
    removed = db.execute(delete(CollectionItem).where(CollectionItem.item_id == tomb.item_id)).rowcount
    if tomb.storage_key is None:
        outcome = CleanupObjectOutcome.NO_OBJECT
    elif _object_in_use(db, tomb.storage_key, tomb.source_file_id):
        outcome = CleanupObjectOutcome.SHARED_KEPT
    else:
        outcome = _remove_object(storage, tomb.storage_key)
    scrubbed_fields, kept = _remove_metadata(db, tomb.item_id, now) if item is not None else ([], [])
    tomb.scrubbed_fields = scrubbed_fields
    tomb.retained_fields = kept
    tomb.purged_at = now  # the DB refuses this while a derived row or a field without basis is left
    tomb.object_outcome = outcome.value
    tomb.cleanup_attempts += 1
    tomb.last_cleanup_at = now
    tomb.last_cleanup_error = None
    record_event(db, action="common_copy.cleanup", outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.SYSTEM,
                 target_type="deletion_tombstone", target_id=str(tomb.id),
                 details={"object_outcome": outcome.value, "memberships_removed": removed,
                          "derived_rows_removed": derived, "embedding_calls_scrubbed": scrubbed,
                          "jobs_cancelled": jobs, "scrubbed_fields": scrubbed_fields, "retained_fields": kept})
    db.commit()
    return outcome.value


# Positive operator capability of the ACTUAL login (no SET ROLE): either the schema owner/migrator
# (superuser or owner of deletion_tombstones), or the provisioned recovery login (app.db_roles: only it
# executes ras_auth_restrict and inserts journal mirrors, and it never executes the runtime-only
# ras_auth_session_create). A failing runtime guard is NOT such evidence.
OPERATOR_PROOF = text("""SELECT current_user = session_user AND (
      r.rolsuper
      OR EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = to_regclass('public.deletion_tombstones') AND c.relowner = r.oid)
      OR (COALESCE(has_function_privilege(to_regprocedure('ras_auth_restrict(uuid)'), 'EXECUTE'), false)
          AND COALESCE(has_table_privilege(to_regclass('public.revocation_journal_records'), 'INSERT'), false)
          AND NOT COALESCE(has_function_privilege(to_regprocedure(
              'ras_auth_session_create(uuid,uuid,uuid,text,text,timestamp with time zone,timestamp with time zone)'),
              'EXECUTE'), true)))
    FROM pg_roles r WHERE r.rolname = current_user""")


def _require_operator(engine: Engine) -> None:
    """Admit only a positively proven owner/recovery login; any probe or release fault refuses.

    Runtime cleanup must use ``app.ingest.cleanup``. Raises before any row lock, mutation or S3.
    """
    db = Session(engine)
    try:
        proven = db.scalar(OPERATOR_PROOF)
    except Exception:
        proven = None
    released = True
    for action in (db.rollback, db.close):
        try:
            action()
        except Exception:
            released = False
    if not released:
        try:
            db.invalidate()
        except Exception:
            pass
        raise CleanupError("operator_identity_unavailable")
    if proven is None:
        raise CleanupError("operator_identity_unavailable")
    if proven is not True:
        raise CleanupError("operator_cleanup_requires_operator")


def operator_cleanup_revoked(engine: Engine, storage: OriginalStorage, now: datetime,
                             batch_size: int) -> dict[str, int]:
    """Operator/recovery purge of unpurged tombstones, oldest deadline first; not a runtime entry.

    Legacy semantics kept for ``app.recovery``: the object is deleted while the
    tombstone/item transaction is open, so it is only safe while API, worker and
    beat are stopped. Idempotent; failures are retried next run.
    """
    _require_operator(engine)
    counts: dict[str, int] = {}
    with Session(engine) as db:
        ids = db.scalars(select(DeletionTombstone.id).where(DeletionTombstone.purged_at.is_(None))
                         .order_by(DeletionTombstone.purge_due_at, DeletionTombstone.id).limit(batch_size)).all()
    for tombstone_id in ids:
        with Session(engine) as db:
            try:
                outcome = _purge_one(db, storage, tombstone_id, now)
            except Exception as exc:
                db.rollback()
                code = exc.code if isinstance(exc, CleanupError) else "cleanup_failed"
                db.execute(update(DeletionTombstone).where(DeletionTombstone.id == tombstone_id,
                                                           DeletionTombstone.purged_at.is_(None)).values(
                    cleanup_attempts=DeletionTombstone.cleanup_attempts + 1, last_cleanup_at=now,
                    last_cleanup_error=code))
                db.commit()
                outcome = f"error:{code}"
        if outcome is not None:
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts
