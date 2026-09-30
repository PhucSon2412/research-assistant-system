"""Recovery gate and journal replay after a restore (M6c-1).

A database or object store restored from before a revocation still holds the
copy as ``listed``, its rights, index and object. Nothing inside that restored
database can say it is stale, so the **gate** compares it with the revocation
journal (``app.journal``), a file kept outside PostgreSQL, the object store
and their backups:

- every revocation appends its journal line (``fsync``) inside the revoking
  transaction, **before** ``t_rev``, and writes the same line to
  ``revocation_journal_records`` in that transaction;
- the gate is open only when the file verifies, belongs to this database's
  registration, the database holds exactly its records (same count, head seq
  and head hash, which covers the whole chain) and no recovery is open.

A restored database is behind the journal, a lost or older journal is behind
the database, another journal does not match the registration: all close the
gate. While closed, the API answers only ``/healthz`` (``/readyz`` is 503),
and Celery tasks, ``process_job`` and each embedding call of the indexer stop
before reading or sending text. ``recover`` (``app.cli.recovery``) replays the
missing records, cleans up, verifies and only then closes its recovery.

A journal line is a decision: once written, the copy it names ends revoked.
If the database commit then fails or its outcome is unknown, the revoking
request replays the line at once; if that fails too the gate stays closed
(journal ahead of the database) until ``recover`` replays it. If the journal
write fails, the revocation still commits (closing the copy at ``t_rev``) and
the request answers 503: the database is then ahead of the journal and the
gate stays closed until an operator repairs the journal
(``recover --confirm-db-is-current``). Only when neither the file nor the
database took the revocation does nothing change (the Admin gets 503).

Locks: the journal advisory lock (``JOURNAL_LOCK``) is exclusive for every
writer (revoke, replay, init, recover) and shared for gate checks, and is
always taken **after** any row lock (item, then rights, then the lock), never
before one, so it cannot close a cycle with publish/revoke/cleanup/index. Gate
checks wait at most ``gate_lock_timeout_ms`` and then count as closed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import journal
from app.audit import record_event
from app.config import Settings
from app.journal import GENESIS, RECOVERY_BEGIN, RECOVERY_END, REVOCATION, JournalError, JournalState, Record
from app.models import (AccessRights, DeletionTombstone, LibraryItem, PaperVersion, RecoveryRegistration,
                        RevocationJournalRecord, RevocationMetadataRetention, SourceFile, User)
from app.models.enums import AuditActorType, AuditOutcome, PublicationState, UserRole, Visibility

log = logging.getLogger(__name__)

# pg advisory lock keys ("rasjrnl" / "rasrcvr" as integers).
JOURNAL_LOCK = 0x7261736A726E6C
RECOVER_LOCK = 0x72617372637672


class RecoveryError(Exception):
    """A recovery step refused or failed; ``code`` is safe to print (IDs and codes only)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GateStatus:
    open: bool
    reason: str
    journal_seq: int | None = None
    db_seq: int | None = None


# --- gate ---------------------------------------------------------------------

def _db_view(conn) -> tuple:
    return tuple(conn.execute(text("""
        SELECT r.journal_id, r.genesis_hash, (SELECT count(*) FROM revocation_journal_records), h.seq, h.record_hash
        FROM (SELECT 1) one
        LEFT JOIN recovery_registrations r ON r.id = 1
        LEFT JOIN LATERAL (SELECT seq, record_hash FROM revocation_journal_records ORDER BY seq DESC LIMIT 1) h
            ON true""")).one())


def compare(state: JournalState, view: tuple) -> GateStatus:
    journal_id, genesis_hash, count, db_seq, db_hash = view
    head = state.head
    closed = lambda reason: GateStatus(False, reason, head.seq, db_seq)  # noqa: E731
    if state.torn is not None:
        return closed("journal_torn")
    if journal_id is None:
        return closed("not_registered")
    if journal_id != state.journal_id or genesis_hash != state.genesis_hash:
        return closed("journal_mismatch")
    if db_seq is None or count != db_seq:
        return closed("db_records_incomplete")
    if db_seq < head.seq:
        return closed("db_behind_journal")
    if db_seq > head.seq:
        return closed("journal_behind_db")
    if db_hash != head.hash:
        return closed("journal_diverged")
    if state.open_recovery is not None:
        return closed("recovery_in_progress")
    return GateStatus(True, "open", head.seq, db_seq)


def evaluate(settings: Settings, engine: Engine) -> GateStatus:
    """Whether content may be served and processed now. Every failure closes the gate."""
    path = settings.recovery.journal_path
    if path is None:
        return GateStatus(False, "journal_not_configured")
    try:
        with engine.connect() as conn, conn.begin():
            conn.execute(text(f"SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}"))
            # Shared: waits out a revocation between its journal line and its commit, never a reader.
            conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": JOURNAL_LOCK})
            view = _db_view(conn)
            try:
                state = journal.read_cached(path)
            except JournalError as exc:
                return GateStatus(False, exc.code, None, view[3])
    except DBAPIError as exc:
        state_code = getattr(exc.orig, "sqlstate", None)
        return GateStatus(False, {"55P03": "journal_lock_busy", "42P01": "db_not_migrated"}.get(
            state_code, "db_unavailable"))
    except Exception:  # noqa: BLE001 - no detail leaves: driver errors may name hosts or users
        return GateStatus(False, "db_unavailable")
    return compare(state, view)


_last_logged: list[str] = []


def log_if_changed(status: GateStatus, where: str) -> None:
    """One warning per change of reason, codes only (no path, no DB detail)."""
    reason = status.reason
    if not _last_logged or _last_logged[-1] != reason:
        _last_logged[:] = [reason]
        if status.open:
            log.warning("recovery gate open (%s)", where)
        else:
            log.warning("recovery gate closed (%s): %s journal_seq=%s db_seq=%s", where, reason,
                        status.journal_seq, status.db_seq)


def gate_open(settings: Settings, engine: Engine, where: str) -> bool:
    status = evaluate(settings, engine)
    if not status.open:
        log_if_changed(status, where)
    return status.open


# --- records ------------------------------------------------------------------

def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else (str(value) if isinstance(value, uuid.UUID) else value)


def revocation_body(tombstone: DeletionTombstone, retentions: list[RevocationMetadataRetention]) -> dict:
    """Identity of a tombstone and its retention bases: what the journal may hold, nothing else."""
    body = {name: _plain(getattr(tombstone, name)) for name in journal.REVOCATION_FIELDS}
    body["retentions"] = [{name: _plain(getattr(b, name)) for name in journal.RETENTION_FIELDS}
                          for b in sorted(retentions, key=lambda r: r.field)]
    return body


def _db_head(db: Session) -> RevocationJournalRecord | None:
    return db.scalar(select(RevocationJournalRecord).order_by(RevocationJournalRecord.seq.desc()).limit(1))


def _lock_journal(db: Session) -> None:
    db.execute(select(func.pg_advisory_xact_lock(JOURNAL_LOCK)))


def _mirror(record: Record, via: str, result: str, tombstone_id: uuid.UUID | None) -> RevocationJournalRecord:
    return RevocationJournalRecord(seq=record.seq, kind=record.kind, record_hash=record.hash, line=record.line,
                                   tombstone_id=tombstone_id, applied_via=via, result=result)


@dataclass(frozen=True)
class Journaled:
    record: Record
    written: bool  # False: the line may be absent or torn; the database will be ahead of the journal


def record_revocation(db: Session, path: Path | None, tombstone: DeletionTombstone,
                      retentions: list[RevocationMetadataRetention]) -> Journaled:
    """In the revoking transaction, after its row locks: mirror the record, then append and fsync it.

    Raises ``RecoveryError`` (nothing written anywhere) when the database and the
    journal do not agree right now: the gate is closed and ``recover`` comes first.
    """
    _lock_journal(db)
    head = _db_head(db)
    if head is None:
        raise RecoveryError("not_registered")
    state = None
    if path is not None:
        try:
            state = journal.read(path)
        except JournalError:
            state = None
    if state is not None and state.torn is None and (state.head.seq != head.seq or state.head.hash != head.record_hash):
        raise RecoveryError("journal_out_of_sync")
    if state is not None and state.torn is not None:
        state = None
    record = journal.make_record(head.seq + 1, REVOCATION, revocation_body(tombstone, retentions), head.record_hash)
    db.add(_mirror(record, "revoke", "revoked", tombstone.id))
    db.flush()
    if state is None:
        return Journaled(record, False)
    try:
        journal.append(path, state, [record])
    except (OSError, JournalError):
        return Journaled(record, False)
    return Journaled(record, True)


# --- replay -------------------------------------------------------------------

def _replay_actor(db: Session, wanted: uuid.UUID, rights: list[AccessRights]) -> uuid.UUID:
    """The recorded Admin if this snapshot has them, else the rights' reviewer, else the first Admin.

    Only the revoked rights and retention rows take the stand-in (their FK and
    trigger need an Admin that exists here); the tombstone keeps the real ID.
    """
    admins = select(User.id).where(User.role == UserRole.ADMIN.value)
    if db.scalar(admins.where(User.id == wanted)) is not None:
        return wanted
    for recorded in rights:
        if db.scalar(admins.where(User.id == recorded.reviewer_user_id)) is not None:
            return recorded.reviewer_user_id
    fallback = db.scalar(admins.order_by(User.id).limit(1))
    if fallback is None:
        raise RecoveryError("no_admin_for_replay")
    return fallback


def _apply_revocation(db: Session, body: dict, item: LibraryItem | None, rights: list[AccessRights],
                      now: datetime) -> tuple[str, uuid.UUID | None, int]:
    from app.publication import _cancel_jobs, _revoke_rows

    tombstone_id = uuid.UUID(body["id"])
    item_id = uuid.UUID(body["item_id"])
    if db.get(DeletionTombstone, tombstone_id) is not None:
        return "tombstone_present", tombstone_id, 0
    if db.scalar(select(DeletionTombstone.id).where(DeletionTombstone.item_id == item_id)) is not None:
        return "superseded", None, 0
    recorded_at = datetime.fromisoformat(body["recorded_at"])
    epoch = int(body["rights_epoch"])
    cancelled = 0
    result = "recorded_item_absent"
    if item is not None:
        # Only a reviewed common copy, and only the version/file the record names: never a private item.
        version_ok = body["version_id"] is None or db.scalar(select(PaperVersion.id).where(
            PaperVersion.id == uuid.UUID(body["version_id"]), PaperVersion.item_id == item.id)) is not None
        file_ok = body["source_file_id"] is None or db.scalar(select(SourceFile.id).where(
            SourceFile.id == uuid.UUID(body["source_file_id"]), SourceFile.item_id == item.id)) is not None
        if item.visibility != Visibility.COMMON.value or not rights or not version_ok or not file_ok:
            raise RecoveryError("record_names_another_item")
        if item.publication_state != PublicationState.REVOKED.value:
            actor = _replay_actor(db, uuid.UUID(body["recorded_by_user_id"]), rights)
            _revoke_rows(db, item, rights, actor, body["reason_code"], recorded_at, max(item.rights_epoch + 1, epoch))
            cancelled = _cancel_jobs(db, item.id, recorded_at)
            result = "revoked" if str(actor) == body["recorded_by_user_id"] else "revoked_actor_substituted"
        else:
            result = "recorded"
    db.add(DeletionTombstone(
        id=tombstone_id, target_kind=body["target_kind"], item_id=item_id,
        version_id=uuid.UUID(body["version_id"]) if body["version_id"] else None,
        source_file_id=uuid.UUID(body["source_file_id"]) if body["source_file_id"] else None,
        storage_key=body["storage_key"],
        access_rights_id=uuid.UUID(body["access_rights_id"]) if body["access_rights_id"] else None,
        reason_code=body["reason_code"], prior_publication_state=body["prior_publication_state"],
        rights_epoch=item.rights_epoch if item is not None else epoch, recorded_at=recorded_at,
        recorded_by_user_id=uuid.UUID(body["recorded_by_user_id"]),
        purge_due_at=datetime.fromisoformat(body["purge_due_at"])))
    db.flush()
    # A basis keeps a field of the copy; with no copy in this snapshot there is nothing to keep.
    if item is not None:
        for basis in body["retentions"]:
            db.add(RevocationMetadataRetention(
                tombstone_id=tombstone_id, item_id=item_id, field=basis["field"],
                policy_source=basis["policy_source"], scope=basis["scope"], reason=basis["reason"],
                confirmed_by_user_id=_replay_actor(db, uuid.UUID(basis["confirmed_by_user_id"]), rights),
                recorded_at=datetime.fromisoformat(basis["recorded_at"])))
        db.flush()
    return result, tombstone_id, cancelled


def apply_record(db: Session, record: Record, now: datetime, via: str = "replay") -> tuple[str, int]:
    """Bring one journal record into the database, in seq order; safe to repeat.

    Locks the named item and its rights first (as revoke does), then the
    journal. Returns the result code and the number of jobs flagged.
    """
    item, rights = None, []
    if record.kind == REVOCATION:
        item = db.scalar(select(LibraryItem).where(LibraryItem.id == uuid.UUID(record.body["item_id"]))
                         .with_for_update().execution_options(populate_existing=True))
        if item is not None:
            rights = list(db.scalars(select(AccessRights).where(AccessRights.item_id == item.id)
                                     .order_by(AccessRights.id).with_for_update()).all())
    _lock_journal(db)
    existing = db.get(RevocationJournalRecord, record.seq)
    if existing is not None:
        if existing.record_hash != record.hash or existing.line != record.line:
            raise RecoveryError("journal_diverged")
        db.rollback()
        return "present", 0
    head = _db_head(db)
    if (head is None and record.seq != 1) or (head is not None and (
            head.seq != record.seq - 1 or head.record_hash != record.prev)):
        raise RecoveryError("journal_gap")
    cancelled, tombstone_id = 0, None
    if record.kind == REVOCATION:
        result, tombstone_id, cancelled = _apply_revocation(db, record.body, item, rights, now)
    else:
        result = record.kind
    db.add(_mirror(record, via, result, tombstone_id))
    db.flush()
    record_event(db, action="recovery.journal_replay", outcome=AuditOutcome.SUCCESS,
                 actor_type=AuditActorType.SYSTEM, target_type="revocation_journal_record", target_id=str(record.seq),
                 details={"kind": record.kind, "result": result, "jobs_cancelled": cancelled})
    db.commit()
    return result, cancelled


def replay_record(engine: Engine, record: Record, now: datetime) -> tuple[str, int]:
    with Session(engine) as db:
        try:
            return apply_record(db, record, now)
        except Exception:
            db.rollback()
            raise


# --- registration -------------------------------------------------------------

def _path(settings: Settings) -> Path:
    if settings.recovery.journal_path is None:
        raise RecoveryError("journal_not_configured")
    return settings.recovery.journal_path


def _registration(db: Session) -> RecoveryRegistration | None:
    return db.get(RecoveryRegistration, 1)


def _unjournaled(db: Session) -> list[DeletionTombstone]:
    return list(db.scalars(select(DeletionTombstone).where(~select(RevocationJournalRecord.seq).where(
        RevocationJournalRecord.tombstone_id == DeletionTombstone.id).exists())
        .order_by(DeletionTombstone.recorded_at, DeletionTombstone.id)).all())


def _body_of(db: Session, tombstone: DeletionTombstone) -> dict:
    bases = list(db.scalars(select(RevocationMetadataRetention)
                            .where(RevocationMetadataRetention.tombstone_id == tombstone.id)).all())
    return revocation_body(tombstone, bases)


def init_new(engine: Engine, settings: Settings, now: datetime) -> Record:
    """Register a **new, empty** database with a new journal. Refuses anything that already has content.

    A restored database is never new: it has items (or a registration), and an
    existing journal file is never replaced.
    """
    path = _path(settings)
    with Session(engine) as db:
        _lock_journal(db)
        if _registration(db) is not None:
            raise RecoveryError("already_registered")
        if (db.scalar(select(func.count()).select_from(LibraryItem)) or
                db.scalar(select(func.count()).select_from(DeletionTombstone)) or _db_head(db) is not None):
            raise RecoveryError("database_not_new")
        if path.exists():
            raise RecoveryError("journal_exists")
        record = journal.genesis(uuid.uuid4(), now, "new_install")
        db.add(RecoveryRegistration(id=1, journal_id=uuid.UUID(record.body["journal_id"]), genesis_hash=record.hash,
                                    mode="new_install", registered_at=now))
        db.add(_mirror(record, "genesis", "new_install", None))
        db.flush()
        try:
            journal.create(path, [record])
        except JournalError as exc:
            raise RecoveryError(exc.code) from None
        db.commit()
        return record


def register_existing(engine: Engine, settings: Settings, now: datetime) -> tuple[Record, int]:
    """Register the **current, live** database (the operator confirms it is not a restored copy).

    Writes a new journal with every existing tombstone, so a later restore of
    an older snapshot replays them too. Returns the genesis and the count imported.
    """
    path = _path(settings)
    with Session(engine) as db:
        _lock_journal(db)
        if _registration(db) is not None:
            raise RecoveryError("already_registered")
        if _db_head(db) is not None:
            raise RecoveryError("database_has_journal_records")
        if path.exists():
            raise RecoveryError("journal_exists")
        records = [journal.genesis(uuid.uuid4(), now, "existing_db")]
        db.add(RecoveryRegistration(id=1, journal_id=uuid.UUID(records[0].body["journal_id"]),
                                    genesis_hash=records[0].hash, mode="existing_db", registered_at=now))
        db.add(_mirror(records[0], "genesis", "existing_db", None))
        for tombstone in _unjournaled(db):
            records.append(journal.next_record(records[-1], REVOCATION, _body_of(db, tombstone)))
            db.add(_mirror(records[-1], "import", "imported", tombstone.id))
        db.flush()
        try:
            journal.create(path, records)
        except JournalError as exc:
            raise RecoveryError(exc.code) from None
        db.commit()
        return records[0], len(records) - 1


# --- recover ------------------------------------------------------------------

@dataclass
class RecoveryReport:
    adopted: bool = False
    journal_rebuilt: int = 0
    journal_repaired: int = 0
    torn_tail_dropped: bool = False
    replayed: dict[str, int] = field(default_factory=dict)
    cleanup: dict[str, int] = field(default_factory=dict)
    objects_removed: int = 0
    gate: str = "closed"


def _records_from_rows(rows: list[RevocationJournalRecord], prev: str) -> list[Record]:
    out = []
    for row in rows:
        try:
            record = journal.parse_line(row.line.encode("ascii"), row.seq, prev)
        except (JournalError, UnicodeEncodeError):
            raise RecoveryError("db_records_corrupt") from None
        if record.hash != row.record_hash:
            raise RecoveryError("db_records_corrupt")
        out.append(record)
        prev = record.hash
    return out


def _repair_journal(engine: Engine, path: Path, now: datetime, confirm_db_current: bool,
                    report: RecoveryReport) -> None:
    """Make the file usable and consistent with the database, or refuse.

    Journal → database is always allowed (replay). Database → journal (a lost,
    truncated or older file) needs ``confirm_db_current``: the operator
    asserts the database is the live one, not a restored copy. A corrupt or
    foreign journal is never rewritten automatically.
    """
    with Session(engine) as db:
        _lock_journal(db)
        registration = _registration(db)
        rows = list(db.scalars(select(RevocationJournalRecord).order_by(RevocationJournalRecord.seq)).all())
        try:
            state = journal.read(path)
        except JournalError as exc:
            if exc.code != "journal_missing":
                raise RecoveryError(exc.code) from None
            if not confirm_db_current:
                raise RecoveryError("journal_missing") from None
            if registration is None or not rows:
                raise RecoveryError("journal_missing_and_database_not_registered") from None
            try:
                state = journal.create(path, _records_from_rows(rows, journal.ZERO_HASH))
            except JournalError as create_exc:
                raise RecoveryError(create_exc.code) from None
            report.journal_rebuilt = len(rows)
        if state.torn is not None:
            # An unfinished append: never durable, never reported as a success. Moved aside, not deleted.
            state = journal.drop_torn_tail(path, state, now.strftime("%Y%m%dT%H%M%S%fZ"))
            report.torn_tail_dropped = True
        if registration is None:
            # A database older than its registration: adopt it and replay the whole journal into it.
            if rows:
                raise RecoveryError("journal_records_without_registration")
            db.add(RecoveryRegistration(id=1, journal_id=state.journal_id, genesis_hash=state.genesis_hash,
                                        mode="adopted", registered_at=now))
            report.adopted = True
        elif registration.journal_id != state.journal_id or registration.genesis_hash != state.genesis_hash:
            raise RecoveryError("journal_mismatch")
        by_seq = {r.seq: r for r in state.records}
        extra = []
        for row in rows:
            record = by_seq.get(row.seq)
            if record is None:
                extra.append(row)
            elif record.hash != row.record_hash or record.line != row.line:
                raise RecoveryError("journal_diverged")
        if extra:
            if not confirm_db_current:
                raise RecoveryError("journal_behind_db")
            try:
                state = journal.append(path, state, _records_from_rows(extra, state.head.hash))
            except JournalError as exc:
                raise RecoveryError(exc.code) from None
            report.journal_repaired += len(extra)
        missing = _unjournaled(db)
        if missing:
            if not confirm_db_current:
                raise RecoveryError("tombstone_not_journaled")
            added = []
            for tombstone in missing:
                added.append(journal.next_record(added[-1] if added else state.head, REVOCATION, _body_of(db, tombstone)))
            state = journal.append(path, state, added)
            report.journal_repaired += len(added)
        db.commit()


def _append_marker(engine: Engine, path: Path, kind: str, body: dict) -> None:
    """Journal-only append under the journal lock; the replay that follows mirrors it."""
    with Session(engine) as db:
        _lock_journal(db)
        try:
            state = journal.read(path)
            journal.append(path, state, [journal.next_record(state.head, kind, body)])
        except JournalError as exc:
            raise RecoveryError(exc.code) from None
        db.commit()


def _replay_all(engine: Engine, now: datetime, path: Path, report: RecoveryReport) -> None:
    try:
        state = journal.read(path)
    except JournalError as exc:
        raise RecoveryError(exc.code) from None
    with Session(engine) as db:
        present = set(db.scalars(select(RevocationJournalRecord.seq)).all())
    for record in state.records:
        if record.seq in present:
            continue
        result, _ = replay_record(engine, record, now)
        report.replayed[result] = report.replayed.get(result, 0) + 1


def _revocations(path: Path) -> list[Record]:
    return [r for r in journal.read(path).records if r.kind == REVOCATION]


def _cleanup(engine: Engine, storage, settings: Settings, now: datetime, report: RecoveryReport) -> None:
    from app.publication import cleanup_revoked

    for _ in range(settings.recovery.cleanup_passes):
        for outcome, count in cleanup_revoked(engine, storage, now, settings.retention.cleanup_batch_size).items():
            report.cleanup[outcome] = report.cleanup.get(outcome, 0) + count
        with Session(engine) as db:
            if not db.scalar(select(func.count()).select_from(DeletionTombstone)
                             .where(DeletionTombstone.purged_at.is_(None))):
                return
    raise RecoveryError("cleanup_incomplete")


def _sweep_objects(engine: Engine, storage, path: Path, report: RecoveryReport) -> None:
    """The object store may be restored apart from the database: every revoked copy's object must be gone."""
    from app.publication import CleanupError, _object_in_use, _remove_object
    from app.models.enums import CleanupObjectOutcome

    with Session(engine) as db:
        for record in _revocations(path):
            key = record.body["storage_key"]
            if key is None:
                continue
            source = uuid.UUID(record.body["source_file_id"]) if record.body["source_file_id"] else None
            if _object_in_use(db, key, source):
                continue
            try:
                if _remove_object(storage, key) == CleanupObjectOutcome.DELETED:
                    report.objects_removed += 1
            except CleanupError:
                raise RecoveryError("object_cleanup_failed") from None


def _verify(engine: Engine, path: Path) -> None:
    with Session(engine) as db:
        if _unjournaled(db):
            raise RecoveryError("tombstone_not_journaled")
        for record in _revocations(path):
            item_id = uuid.UUID(record.body["item_id"])
            tombstone = db.scalar(select(DeletionTombstone).where(DeletionTombstone.item_id == item_id))
            if tombstone is None:
                raise RecoveryError("tombstone_missing")
            if tombstone.purged_at is None:
                raise RecoveryError("cleanup_incomplete")
            item = db.get(LibraryItem, item_id)
            if item is None:
                continue
            if item.publication_state != PublicationState.REVOKED.value:
                raise RecoveryError("copy_not_revoked")
            if db.scalar(text("SELECT ras_common_copy_derived_left(:i)"), {"i": item_id}):
                raise RecoveryError("derived_rows_left")


def recover(engine: Engine, storage, settings: Settings, now: datetime, *,
            confirm_db_current: bool = False) -> RecoveryReport:
    """Verify the journal, replay what the database lacks, clean up, verify, then close the recovery.

    Idempotent: run it again after any failure. The gate stays closed from the
    ``recovery_begin`` record until the ``recovery_end`` record is written and
    mirrored; a failure in between leaves it closed.
    """
    path = _path(settings)
    report = RecoveryReport()
    with engine.connect() as mutex:
        if not mutex.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": RECOVER_LOCK}):
            raise RecoveryError("recovery_already_running")
        mutex.commit()
        try:
            _repair_journal(engine, path, now, confirm_db_current, report)
            recovery_id = journal.read(path).open_recovery
            if recovery_id is None:
                with Session(engine) as db:
                    head = _db_head(db)
                recovery_id = str(uuid.uuid4())
                _append_marker(engine, path, RECOVERY_BEGIN, {"recovery_id": recovery_id, "started_at": now.isoformat(),
                                                              "db_head_seq": head.seq if head else 0})
            _replay_all(engine, now, path, report)
            _cleanup(engine, storage, settings, now, report)
            _sweep_objects(engine, storage, path, report)
            _verify(engine, path)
            purged = sum(v for k, v in report.cleanup.items() if not k.startswith("error:"))
            _append_marker(engine, path, RECOVERY_END, {
                "recovery_id": recovery_id, "finished_at": now.isoformat(),
                "replayed": sum(v for k, v in report.replayed.items() if k not in ("present", "tombstone_present")),
                "tombstones_purged": purged, "objects_removed": report.objects_removed})
            _replay_all(engine, now, path, report)
        finally:
            mutex.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": RECOVER_LOCK})
            mutex.commit()
    status = evaluate(settings, engine)
    report.gate = status.reason
    if not status.open:
        raise RecoveryError(f"gate_still_closed_{status.reason}"[:64])
    return report


def status(engine: Engine, settings: Settings) -> dict:
    """Gate reason and counts for operators: seqs, unpurged and unjournaled tombstones. No content."""
    gate = evaluate(settings, engine)
    out = {"gate": gate.reason, "journal_seq": gate.journal_seq, "db_seq": gate.db_seq}
    try:
        with Session(engine) as db:
            registration = _registration(db)
            out["registered"] = registration.mode if registration else None
            out["unpurged_tombstones"] = db.scalar(select(func.count()).select_from(DeletionTombstone)
                                                   .where(DeletionTombstone.purged_at.is_(None)))
            out["unjournaled_tombstones"] = len(_unjournaled(db))
    except Exception:  # noqa: BLE001
        out["database"] = "unavailable"
    if settings.recovery.journal_path is not None:
        try:
            state = journal.read(settings.recovery.journal_path)
            out["open_recovery"] = state.open_recovery is not None
            out["torn_tail"] = state.torn is not None
        except JournalError as exc:
            out["journal"] = exc.code
    return out
