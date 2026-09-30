"""Publish, revoke and clean up reviewed common copies (M6b-2a).

**Publish** moves a ``pending`` copy (M6b-1) to ``listed``: every User then
sees its *metadata* in the library, and nothing else. ``content_status``
stays ``metadata_only``; there is no job, chunk, index, search hit, RAG
evidence or provider call, and the original stays behind the private-owner
file gate whatever ``permits_original_download`` says. Order:

1. Lock the item, then its rights, ``FOR UPDATE`` (the order revoke and
   tombstone replay use) and only then check state, exactly one live
   AccessRights at the item's epoch, file/version/checksum, the Admin's
   confirmed checksum and no live publication with the same checksum. Every
   refusal is decided on this locked state: a revoke either committed before
   the lock (-> ``common_copy_revoked``) or waits for this transaction. There
   is deliberately no unlocked pre-check: it read the item and the rights in
   two statements, so a revoke committing between them surfaced as
   ``rights_not_publishable``.
2. Still under the lock, read the *common* object back and compare size and
   SHA-256 with the common SourceFile and the rights (never the private
   file's). Missing or different bytes refuse; the item stays ``pending``.
3. Insert ``common_publications`` and set ``listed``. Its partial unique
   index lets one live publication per checksum commit, whatever the
   concurrency; the DB triggers re-check rights and epoch.

**Revoke** (pending or listed) is one transaction whose commit is ``t_rev``:
bump ``rights_epoch``, set ``revoked``, revoke the rights and publication,
flag the item's unfinished jobs ``cancel_requested`` and write a
``deletion_tombstones`` row. From that commit every normal route (library
list/count/detail, collections) drops the item in SQL; file, chunk, search
and RAG never served it. Private items and other common copies are untouched.

**Cleanup** works from unpurged tombstones, each in its own transaction, and
is safe to repeat: it refuses while derived full-text rows exist (none can in
this PR), removes collection memberships, deletes the common object unless a
row that is not revoked names the same key, and **removes the copy's
metadata** (``RevokedMetadataField``: title, source URL, file name, the rights'
source text, licence URL, evidence note and conditions) unless a retention
basis recorded at revocation names that field. Having been publishable is
not such a basis. The DB refuses ``purged_at`` while a field without basis
is left. Only the common copy's rows are touched, never the private item.

Tombstones are never deleted; ``export_tombstones``/``apply_tombstones`` are
a *manual* recovery step after a restore. Until the journal is applied, a
restored backup that predates the revocation serves the copy again.

Audit rows carry IDs and codes only: no title, source, URL, evidence,
checksum, bytes or secrets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.audit import record_event
from app.authz import Principal, review_copy_admin_clause
from app.models import (AccessRights, Chunk, CollectionItem, CommonPublication, DeletionTombstone, IndexGeneration,
                        IngestionJob, LibraryItem, MetadataCandidate, PaperVersion, PdfPage, ReferenceEntry,
                        RevocationMetadataRetention, Section, SourceFile, User)
from app.models.enums import (AuditActorType, AuditOutcome, CleanupObjectOutcome, ContentStatus, PublicationState,
                              RevocationReason, RevokedMetadataField, TombstoneKind, UserRole)
from app.promotion import verified_read
from app.storage import OriginalStorage, is_missing_object


@dataclass(frozen=True)
class Published:
    item: LibraryItem
    publication: CommonPublication


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


def _publishable(db: Session, admin: Principal, item: LibraryItem,
                 confirmed_checksum: str) -> tuple[AccessRights, SourceFile]:
    """Every DB condition for publishing, or a refusal. ``item`` must be locked ``FOR UPDATE``."""
    action = "common_copy.publish"
    if item.publication_state == PublicationState.LISTED.value:
        raise _refuse(db, admin, action, item.id, 409, "already_published", "already_published")
    if item.publication_state != PublicationState.PENDING.value:
        raise _refuse(db, admin, action, item.id, 409, "common_copy_revoked", "revoked")
    rights = db.scalars(select(AccessRights).where(AccessRights.item_id == item.id).order_by(AccessRights.id)
                        .with_for_update().execution_options(populate_existing=True)).all()
    if len(rights) != 1:
        raise _refuse(db, admin, action, item.id, 409, "rights_not_publishable", "rights_count")
    (recorded,) = rights
    if recorded.revoked_at is not None:
        raise _refuse(db, admin, action, item.id, 409, "rights_not_publishable", "rights_revoked")
    if recorded.rights_epoch_at_grant != item.rights_epoch:
        raise _refuse(db, admin, action, item.id, 409, "rights_not_publishable", "epoch_mismatch")
    source = db.get(SourceFile, recorded.source_file_id, populate_existing=True)
    if (source is None or source.item_id != item.id or source.version_id != recorded.version_id
            or source.checksum_sha256 != recorded.file_checksum_sha256):
        raise _refuse(db, admin, action, item.id, 409, "rights_not_publishable", "file_mismatch")
    if item.content_status != ContentStatus.METADATA_ONLY.value or item.active_index_generation_id is not None:
        raise _refuse(db, admin, action, item.id, 409, "rights_not_publishable", "not_metadata_only")
    if confirmed_checksum != recorded.file_checksum_sha256:
        raise _refuse(db, admin, action, item.id, 409, "checksum_confirmation_mismatch",
                      "checksum_confirmation_mismatch")
    if live_duplicate(db, recorded.file_checksum_sha256, item.id) is not None:
        raise _refuse(db, admin, action, item.id, 409, "duplicate_published_checksum", "duplicate_published_checksum")
    return recorded, source


def _check_common_object(db: Session, admin: Principal, storage: OriginalStorage, item_id: uuid.UUID,
                         source: SourceFile, checksum: str) -> None:
    action = "common_copy.publish"
    try:
        intact = verified_read(storage, source.storage_key, source.byte_size, checksum, None)
    except Exception as exc:
        if is_missing_object(exc):
            raise _refuse(db, admin, action, item_id, 409, "common_file_integrity_failed", "object_missing",
                          AuditOutcome.FAILURE) from None
        raise _refuse(db, admin, action, item_id, 503, "publication_storage_unavailable", "storage_unavailable",
                      AuditOutcome.FAILURE) from None
    if not intact:
        raise _refuse(db, admin, action, item_id, 409, "common_file_integrity_failed", "checksum_mismatch",
                      AuditOutcome.FAILURE)


def publish(db: Session, admin: Principal, storage: OriginalStorage, item_id: uuid.UUID, confirmed_checksum: str,
            now: datetime) -> Published:
    item = _copy(db, admin, item_id, lock=True)  # waits for a concurrent revoke, or makes it wait
    if item is None:
        db.rollback()
        raise ApiError(404, "not_found")
    recorded, source = _publishable(db, admin, item, confirmed_checksum)
    # Under the lock: the bytes the common copy serves must still be the reviewed ones.
    _check_common_object(db, admin, storage, item.id, source, recorded.file_checksum_sha256)
    try:
        item.publication_state = PublicationState.LISTED.value
        item.updated_at = now
        db.flush()
        publication = CommonPublication(item_id=item.id, access_rights_id=recorded.id,
                                        file_checksum_sha256=recorded.file_checksum_sha256,
                                        verified_byte_size=source.byte_size, published_at=now,
                                        published_by_user_id=admin.user_id)
        db.add(publication)
        db.flush()  # waits on, then loses to, a concurrent live publication of the same checksum
    except IntegrityError as exc:
        if "uq_common_publications_live_checksum" in str(exc.orig):
            raise _refuse(db, admin, "common_copy.publish", item_id, 409, "duplicate_published_checksum",
                          "duplicate_published_checksum") from None
        # A DB gate (state/rights/epoch trigger) refused what the locked checks allowed; this
        # should not happen, but the answer still follows the committed state, not the guess.
        db.rollback()
        if db.scalar(select(LibraryItem.publication_state).where(LibraryItem.id == item_id)) == \
                PublicationState.REVOKED.value:
            raise _refuse(db, admin, "common_copy.publish", item_id, 409, "common_copy_revoked", "revoked") from None
        raise _refuse(db, admin, "common_copy.publish", item_id, 409, "rights_not_publishable",
                      "db_gate") from None
    _audit(db, admin, "common_copy.publish", AuditOutcome.SUCCESS, item.id, access_rights_id=str(recorded.id),
           source_file_id=str(source.id))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise ApiError(503, "publication_unavailable") from None
    return Published(item, publication)


def _cancel_jobs(db: Session, item_id: uuid.UUID, now: datetime) -> int:
    return db.execute(update(IngestionJob).where(
        IngestionJob.item_id == item_id, IngestionJob.cancel_requested.is_(False),
        IngestionJob.finished_at.is_(None)).values(
        cancel_requested=True, cancel_requested_at=now, updated_at=now)).rowcount


def _revoke_rows(db: Session, item: LibraryItem, rights: list[AccessRights], revoked_by: uuid.UUID,
                 reason: str, at: datetime, epoch: int) -> None:
    """Flip item, rights and publication to revoked; the DB checks it all at commit."""
    item.rights_epoch = epoch
    item.publication_state = PublicationState.REVOKED.value
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
           cleanup_deadline: timedelta, retain: tuple[RetentionBasis, ...] = ()) -> Revoked:
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
    for basis in retain:
        db.add(RevocationMetadataRetention(
            tombstone_id=tombstone.id, item_id=item.id, field=basis.field.value, policy_source=basis.policy_source,
            scope=basis.scope, reason=basis.reason, confirmed_by_user_id=admin.user_id, recorded_at=now))
    db.flush()
    _audit(db, admin, "common_copy.revoke", AuditOutcome.SUCCESS, item.id, reason_code=reason.value,
           from_state=prior, tombstone_id=str(tombstone.id), cancelled_jobs=cancelled,
           retained_fields=sorted(b.field.value for b in retain))
    try:
        db.commit()  # t_rev
    except Exception:
        db.rollback()
        raise ApiError(503, "revocation_unavailable") from None
    return Revoked(item, tombstone, cancelled)


# --- cleanup ------------------------------------------------------------------

# Full-text derivatives a later PR may create for a common copy. This PR creates
# none; cleanup refuses rather than leave them behind or guess their order.
DERIVED_TABLES = (Chunk, Section, PdfPage, ReferenceEntry, MetadataCandidate, IndexGeneration)


class CleanupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _object_in_use(db: Session, key: str, source_file_id: uuid.UUID | None) -> bool:
    """Whether a row that is not a revoked copy (a private file, a live copy) names the key."""
    return db.execute(text("""SELECT 1 FROM source_files f LEFT JOIN library_items i ON i.id = f.item_id
        WHERE f.storage_key = :k AND f.id IS DISTINCT FROM CAST(:f AS uuid)
          AND (i.id IS NULL OR i.publication_state IS DISTINCT FROM 'revoked') LIMIT 1"""),
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
    item = db.get(LibraryItem, tomb.item_id)
    if item is not None and item.publication_state != PublicationState.REVOKED.value:
        raise CleanupError("target_not_revoked")
    for model in DERIVED_TABLES:
        if db.scalar(select(func.count()).select_from(model).where(model.item_id == tomb.item_id)):
            raise CleanupError("derived_rows_present")
    removed = db.execute(delete(CollectionItem).where(CollectionItem.item_id == tomb.item_id)).rowcount
    if tomb.storage_key is None:
        outcome = CleanupObjectOutcome.NO_OBJECT
    elif _object_in_use(db, tomb.storage_key, tomb.source_file_id):
        outcome = CleanupObjectOutcome.SHARED_KEPT
    else:
        outcome = _remove_object(storage, tomb.storage_key)
    scrubbed, kept = _remove_metadata(db, tomb.item_id, now) if item is not None else ([], [])
    tomb.scrubbed_fields = scrubbed
    tomb.retained_fields = kept
    tomb.purged_at = now  # the DB refuses this while a field without basis is left
    tomb.object_outcome = outcome.value
    tomb.cleanup_attempts += 1
    tomb.last_cleanup_at = now
    tomb.last_cleanup_error = None
    record_event(db, action="common_copy.cleanup", outcome=AuditOutcome.SUCCESS, actor_type=AuditActorType.SYSTEM,
                 target_type="deletion_tombstone", target_id=str(tomb.id),
                 details={"object_outcome": outcome.value, "memberships_removed": removed,
                          "scrubbed_fields": scrubbed, "retained_fields": kept})
    db.commit()
    return outcome.value


def cleanup_revoked(engine: Engine, storage: OriginalStorage, now: datetime, batch_size: int) -> dict[str, int]:
    """Purge unpurged tombstones, oldest deadline first. Idempotent; failures are retried next run."""
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


# --- tombstone journal (restore) ----------------------------------------------

JOURNAL_FIELDS = ("id", "target_kind", "item_id", "version_id", "source_file_id", "storage_key",
                  "access_rights_id", "reason_code", "prior_publication_state", "rights_epoch", "recorded_at",
                  "recorded_by_user_id", "purge_due_at")


RETENTION_FIELDS = ("field", "policy_source", "scope", "reason", "confirmed_by_user_id", "recorded_at")


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else (str(value) if isinstance(value, uuid.UUID) else value)


def export_tombstones(db: Session) -> list[dict]:
    """Identity fields of every tombstone and its retention bases: IDs, object key,
    codes, epoch, times and the recorded justification; no paper metadata or content."""
    rows = db.scalars(select(DeletionTombstone).order_by(DeletionTombstone.recorded_at, DeletionTombstone.id)).all()
    out = []
    for row in rows:
        entry = {name: _plain(getattr(row, name)) for name in JOURNAL_FIELDS}
        bases = db.scalars(select(RevocationMetadataRetention).where(RevocationMetadataRetention.tombstone_id == row.id)
                           .order_by(RevocationMetadataRetention.field)).all()
        entry["retentions"] = [{name: _plain(getattr(b, name)) for name in RETENTION_FIELDS} for b in bases]
        out.append(entry)
    return out


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def apply_tombstone(db: Session, entry: dict) -> str:
    """Re-apply one exported tombstone to a (restored) database; safe to repeat."""
    tombstone_id = uuid.UUID(entry["id"])
    if db.get(DeletionTombstone, tombstone_id) is not None:
        db.rollback()
        return "present"
    item_id = uuid.UUID(entry["item_id"])
    recorded_at = datetime.fromisoformat(entry["recorded_at"])
    revoked_by = uuid.UUID(entry["recorded_by_user_id"])
    epoch = int(entry["rights_epoch"])
    item = db.scalar(select(LibraryItem).where(LibraryItem.id == item_id).with_for_update())
    applied = "recorded"
    if item is not None and item.publication_state != PublicationState.REVOKED.value:
        if db.scalar(select(User.id).where(User.id == revoked_by, User.role == UserRole.ADMIN.value)) is None:
            raise ValueError("the revoking Admin is missing from this database")
        rights = list(db.scalars(select(AccessRights).where(AccessRights.item_id == item.id)
                                 .order_by(AccessRights.id).with_for_update()).all())
        if not rights:
            raise ValueError("tombstone names an item that is not a reviewed common copy")
        _revoke_rows(db, item, rights, revoked_by, entry["reason_code"], recorded_at,
                     max(item.rights_epoch + 1, epoch))
        _cancel_jobs(db, item.id, recorded_at)
        applied = "revoked"
    db.add(DeletionTombstone(
        id=tombstone_id, target_kind=entry["target_kind"], item_id=item_id, version_id=_uuid(entry["version_id"]),
        source_file_id=_uuid(entry["source_file_id"]), storage_key=entry["storage_key"],
        access_rights_id=_uuid(entry["access_rights_id"]), reason_code=entry["reason_code"],
        prior_publication_state=entry["prior_publication_state"],
        rights_epoch=item.rights_epoch if item is not None else epoch, recorded_at=recorded_at,
        recorded_by_user_id=revoked_by, purge_due_at=datetime.fromisoformat(entry["purge_due_at"])))
    db.flush()
    for basis in entry.get("retentions", []):
        db.add(RevocationMetadataRetention(
            tombstone_id=tombstone_id, item_id=item_id, field=basis["field"], policy_source=basis["policy_source"],
            scope=basis["scope"], reason=basis["reason"], confirmed_by_user_id=uuid.UUID(basis["confirmed_by_user_id"]),
            recorded_at=datetime.fromisoformat(basis["recorded_at"])))
    db.flush()
    record_event(db, action="common_copy.tombstone_replay", outcome=AuditOutcome.SUCCESS,
                 actor_type=AuditActorType.SYSTEM, target_type="deletion_tombstone", target_id=str(tombstone_id),
                 details={"result": applied})
    db.commit()
    return applied


def apply_tombstones(engine: Engine, entries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        with Session(engine) as db:
            result = apply_tombstone(db, entry)
        counts[result] = counts.get(result, 0) + 1
    return counts
