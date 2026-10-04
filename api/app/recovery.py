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
  and head hash, which covers the whole chain), every one of them is
  ``journal_confirmed`` (written *and* ``fsync`` returned) and no recovery is
  open.

A restored database is behind the journal, a lost or older journal is behind
the database, another journal does not match the registration: all close the
gate. While closed, the API answers only ``/healthz`` (``/readyz`` is 503),
and Celery tasks, ``process_job`` and each embedding call of the indexer stop
before reading or sending text. ``recover`` (``app.cli.recovery``) replays the
missing records, cleans up, verifies and only then closes its recovery.
``hold`` (M6c-2a) opens that recovery right after a restore, so the gate is shut
even when the restored database happens to match the journal.

Review grants (M6c-2b1): closing a review request (withdraw, reject,
approve) ends the Admin's temporary read of a private original. The closing
transaction journals a ``review_closure`` record exactly as a revocation
journals its tombstone (same lock order, failure windows and
``journal_confirmed`` rule), and the database refuses a close without it. A
replay into a snapshot that still has the request open writes
``closed_by_recovery``: never an approval, never a common copy. Closes from
before 0028 have no record; the gate stays closed
(``review_closures_not_journaled``) until ``recover`` appends them.

A journal line is a decision: once written, the copy it names ends revoked.
If the database commit then fails or its outcome is unknown, the revoking
request replays the line at once; if that fails too the gate stays closed
(journal ahead of the database) until ``recover`` replays it. If the journal
write fails in any way, the revocation still commits (closing the copy at
``t_rev``) with its record ``journal_confirmed = false``, and the request
answers 503. That covers a complete line whose ``fsync`` raised: the file and
the database then agree, but the line was never confirmed durable, so the
gate stays closed (``journal_unconfirmed``) until ``recover`` syncs the file
itself and confirms the record. If the line is missing or torn, the database
is ahead of the journal and repairing it needs ``recover
--confirm-db-is-current``. Only when neither the file nor the database took
the revocation does nothing change (the Admin gets 503).

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

from sqlalchemy import Engine, func, select, text, update, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, defer

from app import journal
from app.audit import record_event
from app.config import Settings
from app.journal import (GENESIS, RECOVERY_BEGIN, RECOVERY_END, REVIEW_CLOSURE, REVOCATION, SESSION_REVOCATION, RESET_TOKEN_CLOSURE, INVITATION_CLOSURE, JournalError,
                         JournalState, Record)
from app.models import (AccessRights, DeletionTombstone, LibraryItem, PaperVersion, RecoveryRegistration,
                        ReviewRequest, RevocationJournalRecord, RevocationMetadataRetention, SourceFile, User, UserSession,
                        PasswordResetToken, Invitation)
from app.models.enums import AuditActorType, AuditOutcome, PublicationState, ReviewStatus, UserRole, Visibility
from app.models.review import closed_status_of

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

# Closed review requests the journal does not know about (only closes committed before 0028 can be).
UNJOURNALED_CLOSURES_SQL = """SELECT count(*) FROM review_requests rr WHERE rr.status <> 'open' AND NOT EXISTS
    (SELECT 1 FROM revocation_journal_records j WHERE j.review_request_id = rr.id)"""
UNJOURNALED_SESSIONS_SQL = """SELECT count(*) FROM sessions s WHERE s.revoked_at IS NOT NULL AND NOT EXISTS
    (SELECT 1 FROM revocation_journal_records j WHERE j.session_id = s.id)"""
UNJOURNALED_RESET_TOKENS_SQL = """SELECT count(*) FROM password_reset_tokens t
    WHERE (t.used_at IS NOT NULL OR t.revoked_at IS NOT NULL) AND NOT EXISTS
    (SELECT 1 FROM revocation_journal_records j WHERE j.reset_token_id = t.id)"""


UNJOURNALED_INVITATIONS_SQL = """SELECT count(*) FROM invitations i
    WHERE num_nonnulls(i.accepted_at,i.revoked_at,i.closed_by_recovery_at) > 0 AND NOT EXISTS
    (SELECT 1 FROM revocation_journal_records j WHERE j.invitation_id = i.id)"""


def _db_view(conn) -> tuple:
    return tuple(conn.execute(text(f"""
        SELECT r.journal_id, r.genesis_hash, (SELECT count(*) FROM revocation_journal_records), h.seq, h.record_hash,
               (SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed),
               ({UNJOURNALED_CLOSURES_SQL}), ({UNJOURNALED_SESSIONS_SQL}), ({UNJOURNALED_RESET_TOKENS_SQL}), ({UNJOURNALED_INVITATIONS_SQL})
        FROM (SELECT 1) one
        LEFT JOIN recovery_registrations r ON r.id = 1
        LEFT JOIN LATERAL (SELECT seq, record_hash FROM revocation_journal_records ORDER BY seq DESC LIMIT 1) h
            ON true""")).one())


def compare(state: JournalState, view: tuple) -> GateStatus:
    journal_id, genesis_hash, count, db_seq, db_hash, unconfirmed, unjournaled_closures, unjournaled_sessions, unjournaled_tokens, unjournaled_invitations = view
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
    if unconfirmed:
        # File and database agree, but a write was never confirmed durable (e.g. fsync raised).
        return closed("journal_unconfirmed")
    if unjournaled_closures:
        # Closed before 0028: a restore of an older snapshot would reopen those grants until they are journaled.
        return closed("review_closures_not_journaled")
    if unjournaled_sessions:
        return closed("sessions_not_journaled")
    if unjournaled_tokens:
        return closed("reset_tokens_not_journaled")
    if unjournaled_invitations:
        return closed("invitations_not_journaled")
    if state.open_recovery is not None:
        return closed("recovery_in_progress")
    return GateStatus(True, "open", head.seq, db_seq)


def evaluate_integrity(settings: Settings, engine: Engine) -> GateStatus:
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
        return GateStatus(False, {"55P03": "journal_lock_busy", "42P01": "db_not_migrated",
                                  "42703": "db_not_migrated"}.get(
            state_code, "db_unavailable"))
    except Exception:  # noqa: BLE001 - no detail leaves: driver errors may name hosts or users
        return GateStatus(False, "db_unavailable")
    return compare(state, view)


_last_logged: list[str] = []


def evaluate(settings: Settings, engine: Engine) -> GateStatus:
    integrity = evaluate_integrity(settings, engine)
    if not integrity.open:
        return integrity
    try:
        from app.auth_safety import cutover
        with Session(engine) as db:
            reason = cutover(db)
    except Exception:
        return GateStatus(False, "auth_safety_version_missing", integrity.journal_seq, integrity.db_seq)
    return GateStatus(reason == "ready", reason, integrity.journal_seq, integrity.db_seq)


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


def closure_body(rr: ReviewRequest) -> dict:
    """The end of one review grant: IDs, decision, code and time; never text, note, title, email or checksum."""
    return {"request_id": str(rr.id), "item_id": str(rr.item_id), "version_id": str(rr.version_id),
            "source_file_id": str(rr.source_file_id), "status": rr.status, "reason_code": rr.close_reason_code,
            "closed_at": rr.closed_at.isoformat()}


def _db_head(db: Session):
    """``(seq, record_hash)`` of the last mirrored record: only columns every journal revision (0026+) has,
    so ``hold`` works on a restored snapshot before it is migrated."""
    return db.execute(select(RevocationJournalRecord.seq, RevocationJournalRecord.record_hash)
                      .order_by(RevocationJournalRecord.seq.desc()).limit(1)).first()


def _lock_journal(db: Session) -> None:
    db.execute(select(func.pg_advisory_xact_lock(JOURNAL_LOCK)))


def _mirror(record: Record, via: str, result: str, tombstone_id: uuid.UUID | None, *,
            confirmed: bool, review_request_id: uuid.UUID | None = None,
            session_id: uuid.UUID | None = None, session_user_id: uuid.UUID | None = None) -> RevocationJournalRecord:
    """``confirmed``: this line is in the file and an ``fsync`` of it has returned."""
    token_fields = {} if record.kind != RESET_TOKEN_CLOSURE else dict(
        reset_token_id=uuid.UUID(record.body["token_id"]), reset_token_user_id=uuid.UUID(record.body["user_id"]),
        reset_end_kind=record.body["end_kind"], reset_reason_code=record.body["reason_code"],
        reset_ended_at=datetime.fromisoformat(record.body["ended_at"]))
    invitation_fields = {} if record.kind != INVITATION_CLOSURE else dict(
        invitation_id=uuid.UUID(record.body["invitation_id"]), invitation_end_kind=record.body["end_kind"],
        invitation_reason_code=record.body["reason_code"], invitation_ended_at=datetime.fromisoformat(record.body["ended_at"]))
    auth_fields = {} if record.kind not in journal.AUTH_KINDS else dict(
        auth_user_id=uuid.UUID(record.body["user_id"]), auth_operation_id=uuid.UUID(record.body["operation_id"]),
        auth_generation=uuid.UUID(record.body["generation"]),
        auth_domain="credential" if record.kind == journal.CREDENTIAL_TRANSITION else "status",
        auth_decision="changed" if record.kind == journal.CREDENTIAL_TRANSITION else record.body["decision"],
        auth_reason_code=record.body["reason_code"], auth_recorded_at=datetime.fromisoformat(record.body["recorded_at"]))
    return RevocationJournalRecord(seq=record.seq, kind=record.kind, record_hash=record.hash, line=record.line,
                                   tombstone_id=tombstone_id, review_request_id=review_request_id, applied_via=via,
                                   session_id=session_id, session_user_id=session_user_id,
                                   result=result, journal_confirmed=confirmed, **token_fields, **invitation_fields, **auth_fields)


def _unconfirmed(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(RevocationJournalRecord)
                     .where(RevocationJournalRecord.journal_confirmed.is_(False)))


@dataclass(frozen=True)
class Journaled:
    record: Record
    # False: the line may be absent, torn, or complete but not confirmed durable (fsync raised). The
    # record then commits unconfirmed and the gate stays closed until `recover` confirms it.
    written: bool


def _journal_decision(db: Session, path: Path | None, kind: str, body: dict, via: str, result: str, *,
                      tombstone_id: uuid.UUID | None = None, review_request_id: uuid.UUID | None = None) -> Journaled:
    """In the deciding transaction, after its row locks: mirror the record, then append and fsync it.

    The mirror row is inserted ``journal_confirmed = false`` and set ``true``
    only after ``journal.append`` returned (line written and ``fsync`` done).
    Raises ``RecoveryError`` (nothing written anywhere) when the database and the
    journal do not agree right now, or an earlier write is still unconfirmed:
    the gate is closed and ``recover`` comes first.
    """
    _lock_journal(db)
    head = _db_head(db)
    if head is None:
        raise RecoveryError("not_registered")
    if _unconfirmed(db):
        raise RecoveryError("journal_unconfirmed")
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
    record = journal.make_record(head.seq + 1, kind, body, head.record_hash)
    row = _mirror(record, via, result, tombstone_id, confirmed=False, review_request_id=review_request_id)
    db.add(row)
    db.flush()
    if state is None:
        return Journaled(record, False)
    try:
        journal.append(path, state, [record])
    except (OSError, JournalError):
        # Absent, torn, or complete but unsynced: none of them is a confirmed durable line.
        return Journaled(record, False)
    row.journal_confirmed = True  # written and fsynced; the only update the DB allows on a record
    db.flush()
    return Journaled(record, True)


def record_revocation(db: Session, path: Path | None, tombstone: DeletionTombstone,
                      retentions: list[RevocationMetadataRetention]) -> Journaled:
    """The revocation's record (M6c-1), in the revoking transaction; see ``_journal_decision``."""
    return _journal_decision(db, path, REVOCATION, revocation_body(tombstone, retentions), "revoke", "revoked",
                             tombstone_id=tombstone.id)


def record_review_closure(db: Session, path: Path | None, rr: ReviewRequest) -> Journaled:
    """The close of a review grant (M6c-2b1), once ``rr`` is locked ``FOR UPDATE`` and set closed.

    Same rules as ``record_revocation``: after the row locks (request, and for
    approve the private item), before the commit that ends the grant.
    """
    return _journal_decision(db, path, REVIEW_CLOSURE, closure_body(rr), "close", "closed",
                             review_request_id=rr.id)


def session_body(row: UserSession) -> dict:
    return {"session_id": str(row.id), "user_id": str(row.user_id), "reason_code": row.revoked_reason,
            "revoked_at": row.revoked_at.isoformat()}


def record_session_revocations(db: Session, path: Path | None, rows: list[UserSession]) -> list[Journaled]:
    return record_auth_endings(db, path, [], rows)


def reset_token_body(row: PasswordResetToken) -> dict:
    kind = "consumed" if row.used_at is not None else "revoked"
    return {"token_id": str(row.id), "user_id": str(row.user_id), "end_kind": kind,
            "reason_code": row.closure_reason or f"legacy_{kind}",
            "ended_at": (row.used_at or row.revoked_at).isoformat()}


def invitation_body(row: Invitation) -> dict:
    kind = row.recovery_end_kind or ("accepted" if row.accepted_at is not None else "revoked")
    return {"invitation_id": str(row.id), "end_kind": kind,
            "reason_code": row.closure_reason or f"legacy_{kind}",
            "ended_at": (row.accepted_at or row.revoked_at or row.closed_by_recovery_at).isoformat()}


def record_auth_endings(db: Session, path: Path | None, tokens: list[PasswordResetToken],
                        rows: list[UserSession], *, invitations: list[Invitation] | None = None, during_recovery: bool = False, auth_changes=None) -> list[Journaled]:
    """One batch after ALL invitation/email or user/token/session locks. Mirror all lines before one append/fsync.

    A failed append leaves ALL rows unconfirmed, including any complete prefix.
    Commit still revokes the whole batch atomically; recover repairs missing
    lines from the live DB (confirmation required) or replays a durable prefix
    after rollback. No further auth row locks may be taken after this call.
    """
    invitations = invitations or []
    if not rows and not tokens and not invitations and not auth_changes:
        return []
    _lock_journal(db)
    head = _db_head(db)
    if head is None or _unconfirmed(db):
        raise RecoveryError("journal_unconfirmed" if head else "not_registered")
    state = None
    if path is not None:
        try:
            state = journal.read(path)
        except JournalError:
            pass
    if state is not None and state.torn is None and (state.head.seq != head.seq or state.head.hash != head.record_hash):
        raise RecoveryError("journal_out_of_sync")
    if state is not None and (state.torn is not None or (state.open_recovery is not None and not during_recovery)):
        raise RecoveryError("recovery_in_progress")
    records, legacy_mirrors = [], []
    routines = db.scalar(text("SELECT to_regprocedure('ras_auth_closure_mirror(text,text,text)') IS NOT NULL"))
    if auth_changes:
        if state is None or state.torn is not None:
            raise RecoveryError("auth_journal_unavailable")
        from app.auth_safety import stage
        records = stage(db, state, auth_changes)
    for row in [*invitations, *tokens, *rows]:
        if isinstance(row, Invitation):
            kind, body = INVITATION_CLOSURE, invitation_body(row)
        elif isinstance(row, PasswordResetToken):
            kind, body = RESET_TOKEN_CLOSURE, reset_token_body(row)
        else:
            kind, body = SESSION_REVOCATION, session_body(row)
        record = journal.make_record(head.seq + 1, kind, body, head.record_hash) if not records \
            else journal.next_record(records[-1], kind, body)
        records.append(record)
        is_session = kind == SESSION_REVOCATION
        via = "import" if during_recovery else "revoke" if is_session else "close"
        result = "imported" if during_recovery else "session_revoked" if is_session else "invitation_ended" if kind == INVITATION_CLOSURE else "reset_token_ended"
        if routines:
            db.execute(text("SELECT ras_auth_closure_mirror(:line,:via,:result)"),
                       {"line": record.line, "via": via, "result": result})
        else:
            # Pre-0035 recovery/migration compatibility only. Runtime has no raw grant.
            mirror = _mirror(record, via, result, None, confirmed=False,
                             session_id=row.id if is_session else None, session_user_id=row.user_id if is_session else None)
            legacy_mirrors.append(mirror)
            db.add(mirror)
    db.flush()  # CHECK and target trigger run BEFORE any byte is appended.
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    db.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    written = False
    if state is not None:
        try:
            journal.append(path, state, records)
            written = True
        except (OSError, JournalError):
            pass
    if written:
        for record in records:
            if routines or record.kind in journal.AUTH_KINDS:
                db.execute(text("SELECT ras_auth_confirm(:seq)"), {"seq": record.seq})
        for mirror in legacy_mirrors:
            mirror.journal_confirmed = True
        db.flush()
    return [Journaled(record, written) for record in records]


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


def _apply_review_closure(db: Session, body: dict, rr: ReviewRequest | None) -> tuple[str, uuid.UUID | None]:
    """End the grant a closure record names, in a snapshot that may still have the request open.

    Writes ``closed_by_recovery`` with the recorded code and time: no actor, no
    note, no rights and no common copy, so an approval whose copy the snapshot
    lacks is not re-created and the replay is not an approval. A request the
    snapshot already has closed, in any state, is left as it is; one it lacks
    gets only the mirror row (no FK). Never touches the item, versions or files.
    """
    request_id = uuid.UUID(body["request_id"])
    if db.scalar(select(RevocationJournalRecord.seq).where(RevocationJournalRecord.review_request_id == request_id)):
        return "superseded", None
    if rr is None:
        return "recorded_request_absent", request_id
    if (str(rr.item_id), str(rr.version_id), str(rr.source_file_id)) != (
            body["item_id"], body["version_id"], body["source_file_id"]):
        raise RecoveryError("record_names_another_request")
    if rr.status != ReviewStatus.OPEN.value:
        return "already_closed", request_id
    rr.status = ReviewStatus.CLOSED_BY_RECOVERY.value
    rr.close_reason_code = body["reason_code"]
    rr.closed_at = datetime.fromisoformat(body["closed_at"])
    rr.closed_by_user_id = None
    rr.rejection_note = None
    db.flush()
    return "closed", request_id


def _apply_reset_token_closure(row: PasswordResetToken | None, body: dict, *, apply: bool = True) -> str:
    if row is None:
        return "recorded_reset_token_absent"
    if str(row.user_id) != body["user_id"]:
        raise RecoveryError("record_names_another_reset_token_user")
    if row.used_at is not None or row.revoked_at is not None:
        if reset_token_body(row) != body:
            # Compare timestamps as instants, including equivalent timezone spellings.
            actual = reset_token_body(row)
            if (actual | {"ended_at": body["ended_at"]}) != body or (
                    row.used_at or row.revoked_at) != datetime.fromisoformat(body["ended_at"]):
                raise RecoveryError("reset_token_closure_conflict")
        return "reset_token_already_ended"
    if not apply:
        raise RecoveryError("reset_token_not_ended")
    if body["end_kind"] == "consumed":
        row.used_at = datetime.fromisoformat(body["ended_at"])
    else:
        row.revoked_at = datetime.fromisoformat(body["ended_at"])
    row.closure_reason = body["reason_code"]
    return "reset_token_ended"


def _apply_invitation_closure(row: Invitation | None, body: dict, *, apply: bool = True) -> str:
    if row is None:
        return "recorded_invitation_absent"
    if row.accepted_at or row.revoked_at or row.closed_by_recovery_at:
        actual = invitation_body(row)
        if ((actual | {"ended_at": body["ended_at"]}) != body or
                (row.accepted_at or row.revoked_at or row.closed_by_recovery_at) != datetime.fromisoformat(body["ended_at"])):
            raise RecoveryError("invitation_closure_conflict")
        return "invitation_already_ended"
    if not apply:
        raise RecoveryError("invitation_not_ended")
    row.closed_by_recovery_at = datetime.fromisoformat(body["ended_at"])
    row.recovery_end_kind, row.closure_reason = body["end_kind"], body["reason_code"]
    # Never synthesize accepted_at/accepted_user_id or an identity account.
    return "invitation_closed_by_recovery"


def apply_record(db: Session, record: Record, now: datetime, via: str = "replay") -> tuple[str, int]:
    """Bring one journal record into the database, in seq order; safe to repeat.

    Locks the named item and its rights first (as revoke does), then the
    journal. Returns the result code and the number of jobs flagged. The
    mirror row is ``journal_confirmed``: callers only replay lines whose file
    was fsynced (the revoke that just appended it, or ``recover`` after its
    own ``journal.sync``).
    """
    # Validate even direct replay callers, not just lines read from the file.
    if journal.make_record(record.seq, record.kind, record.body, record.prev) != record:
        raise RecoveryError("journal_corrupt")
    item, rights, rr, session_row, token_row, invitation_row = None, [], None, None, None, None
    if record.kind in journal.AUTH_KINDS:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uuid.UUID(record.body["user_id"])]})
    if record.kind == INVITATION_CLOSURE:
        from app.identity.invitation_endings import lock_email
        iid = uuid.UUID(record.body["invitation_id"])
        email = db.scalar(select(Invitation.email).where(Invitation.id == iid))
        if email is not None:
            lock_email(db, email)
        invitation_routines = db.scalar(text("SELECT to_regprocedure('ras_auth_invitation_lock(uuid[])') IS NOT NULL"))
        if invitation_routines:
            db.execute(text("SELECT ras_auth_invitation_lock(:ids)"), {"ids": [iid]})
        stmt = select(Invitation).options(*_invitation_recovery_columns()).where(Invitation.id == iid)
        invitation_row = db.scalar((stmt if invitation_routines else stmt.with_for_update())
                                   .execution_options(populate_existing=True))
    if record.kind == RESET_TOKEN_CLOSURE:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uuid.UUID(record.body["user_id"])]})
        token_routines = db.scalar(text("SELECT to_regprocedure('ras_auth_token_lock(uuid,uuid[])') IS NOT NULL"))
        if token_routines:
            db.execute(text("SELECT ras_auth_token_lock(:uid,:ids)"),
                       {"uid": uuid.UUID(record.body["user_id"]), "ids": [uuid.UUID(record.body["token_id"])]})
        stmt = select(PasswordResetToken).where(PasswordResetToken.id == uuid.UUID(record.body["token_id"]))
        token_row = db.scalar((stmt if token_routines else stmt.with_for_update()).execution_options(populate_existing=True))
    if record.kind == SESSION_REVOCATION:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uuid.UUID(record.body["user_id"])]})
        routines = db.scalar(text("SELECT to_regprocedure('ras_auth_session_lock(uuid,uuid[])') IS NOT NULL"))
        if routines:
            db.execute(text("SELECT ras_auth_session_lock(:uid,:ids)"),
                       {"uid": uuid.UUID(record.body["user_id"]), "ids": [uuid.UUID(record.body["session_id"])]})
        stmt = select(UserSession).where(UserSession.id == uuid.UUID(record.body["session_id"]))
        session_row = db.scalar((stmt if routines else stmt.with_for_update()).execution_options(populate_existing=True))
        if session_row is not None and str(session_row.user_id) != record.body["user_id"]:
            raise RecoveryError("record_names_another_session_user")
    if record.kind == REVIEW_CLOSURE:
        # The request row first, as withdraw/reject/approve lock it, then the journal.
        rr = db.scalar(select(ReviewRequest).where(ReviewRequest.id == uuid.UUID(record.body["request_id"]))
                       .with_for_update().execution_options(populate_existing=True))
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
        if record.kind == INVITATION_CLOSURE:
            _apply_invitation_closure(invitation_row, record.body, apply=False)
        if record.kind == RESET_TOKEN_CLOSURE:
            _apply_reset_token_closure(token_row, record.body, apply=False)
        if record.kind == SESSION_REVOCATION and session_row is not None:
            if (session_row.revoked_at,session_row.revoked_reason) != (datetime.fromisoformat(record.body["revoked_at"]),record.body["reason_code"]):
                raise RecoveryError("session_revocation_conflict")
        if record.kind in journal.AUTH_KINDS:
            db.execute(text("SELECT ras_auth_project(:uid)"), {"uid": uuid.UUID(record.body["user_id"])})
            db.commit()
        else:
            db.rollback()
        return "present", 0
    head = _db_head(db)
    if (head is None and record.seq != 1) or (head is not None and (
            head.seq != record.seq - 1 or head.record_hash != record.prev)):
        raise RecoveryError("journal_gap")
    cancelled, tombstone_id, request_id = 0, None, None
    if record.kind == REVOCATION:
        result, tombstone_id, cancelled = _apply_revocation(db, record.body, item, rights, now)
    elif record.kind == REVIEW_CLOSURE:
        result, request_id = _apply_review_closure(db, record.body, rr)
    elif record.kind == INVITATION_CLOSURE:
        if invitation_routines and invitation_row is not None and not (
                invitation_row.accepted_at or invitation_row.revoked_at or invitation_row.closed_by_recovery_at):
            db.execute(text("SELECT ras_auth_invitation_recover(:id,:kind,:reason,:at)"),
                       {"id": invitation_row.id, "kind": record.body["end_kind"],
                        "reason": record.body["reason_code"], "at": datetime.fromisoformat(record.body["ended_at"])})
            db.refresh(invitation_row)
            _apply_invitation_closure(invitation_row, record.body, apply=False)
            result = "invitation_closed_by_recovery"
        else:
            result = _apply_invitation_closure(invitation_row, record.body, apply=not invitation_routines)
    elif record.kind == RESET_TOKEN_CLOSURE:
        if token_routines and token_row is not None and token_row.used_at is None and token_row.revoked_at is None:
            db.execute(text("SELECT ras_auth_token_close(:id,:uid,:kind,:reason,:at)"),
                       {"id": token_row.id, "uid": uuid.UUID(record.body["user_id"]),
                        "kind": record.body["end_kind"], "reason": record.body["reason_code"],
                        "at": datetime.fromisoformat(record.body["ended_at"])})
            db.refresh(token_row)
            _apply_reset_token_closure(token_row, record.body, apply=False)
            result = "reset_token_ended"
        else:
            result = _apply_reset_token_closure(token_row, record.body, apply=not token_routines)
    elif record.kind == SESSION_REVOCATION:
        result = "recorded_session_absent"
        if session_row is not None:
            stamp = datetime.fromisoformat(record.body["revoked_at"])
            if session_row.revoked_at is None:
                if routines:
                    db.execute(text("SELECT ras_auth_session_close(:id,:uid,:at,:reason)"), {
                        "id": session_row.id, "uid": session_row.user_id, "at": stamp, "reason": record.body["reason_code"]})
                else:
                    session_row.revoked_at, session_row.revoked_reason = stamp, record.body["reason_code"]
                result = "session_revoked"
            elif (session_row.revoked_at, session_row.revoked_reason) != (stamp, record.body["reason_code"]):
                raise RecoveryError("session_revocation_conflict")
            else:
                result = "session_already_revoked"
    else:
        result = record.kind
    if ((record.kind == SESSION_REVOCATION and routines) or (record.kind == RESET_TOKEN_CLOSURE and token_routines)
            or (record.kind == INVITATION_CLOSURE and invitation_routines)):
        db.execute(text("SELECT ras_auth_closure_mirror(:line,:via,:result)"), {"line": record.line,"via":via,"result":result})
        db.execute(text("SELECT ras_auth_confirm(:seq)"), {"seq": record.seq})
    else:
        db.add(_mirror(record, via, result, tombstone_id, confirmed=True, review_request_id=request_id,
                       session_id=uuid.UUID(record.body["session_id"]) if record.kind == SESSION_REVOCATION else None,
                       session_user_id=uuid.UUID(record.body["user_id"]) if record.kind == SESSION_REVOCATION else None))
    db.flush()
    if record.kind in journal.AUTH_KINDS:
        db.execute(text("SELECT ras_auth_project(:uid)"), {"uid": uuid.UUID(record.body["user_id"])})
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


def _unjournaled_closures(db: Session) -> list[ReviewRequest]:
    """Closed requests without a mirrored record: only closes committed before 0028 can be."""
    return list(db.scalars(select(ReviewRequest).where(
        ReviewRequest.status != ReviewStatus.OPEN.value,
        ~select(RevocationJournalRecord.seq).where(RevocationJournalRecord.review_request_id == ReviewRequest.id)
        .exists()).order_by(ReviewRequest.closed_at, ReviewRequest.id)).all())


def _unjournaled_sessions(db: Session) -> list[UserSession]:
    return list(db.scalars(select(UserSession).where(UserSession.revoked_at.is_not(None),
        ~select(RevocationJournalRecord.seq).where(RevocationJournalRecord.session_id == UserSession.id).exists())
        .order_by(UserSession.user_id, UserSession.id)).all())


def _unjournaled_reset_tokens(db: Session) -> list[PasswordResetToken]:
    return list(db.scalars(select(PasswordResetToken).where(
        or_(PasswordResetToken.used_at.is_not(None), PasswordResetToken.revoked_at.is_not(None)),
        ~select(RevocationJournalRecord.seq).where(RevocationJournalRecord.reset_token_id == PasswordResetToken.id).exists())
        .order_by(PasswordResetToken.user_id, PasswordResetToken.id)))


def _invitation_recovery_columns():
    # Closure replay/backfill also runs on pre-0039 snapshots. Issuance/pair
    # transaction markers are irrelevant to recovery and must never lazy-load.
    return (defer(Invitation.creation_transaction_id, raiseload=True),
            defer(Invitation.acceptance_transaction_id, raiseload=True))


def _unjournaled_invitations(db: Session) -> list[Invitation]:
    return list(db.scalars(select(Invitation).options(*_invitation_recovery_columns()).where(
        or_(Invitation.accepted_at.is_not(None), Invitation.revoked_at.is_not(None),
            Invitation.closed_by_recovery_at.is_not(None)),
        ~select(RevocationJournalRecord.seq).where(RevocationJournalRecord.invitation_id == Invitation.id).exists())
        .order_by(Invitation.id)))


def _lock_legacy_sessions(db: Session) -> None:
    """Users before emails, matching live Admin writers; recover holds the gate closed."""
    from app.identity.invitation_endings import lock_email
    owners = list(db.scalars(select(UserSession.user_id).where(UserSession.revoked_at.is_not(None)).distinct()))
    ended = or_(PasswordResetToken.used_at.is_not(None), PasswordResetToken.revoked_at.is_not(None))
    owners += list(db.scalars(select(PasswordResetToken.user_id).where(ended).distinct()))
    db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": sorted(set(owners))})
    for email in sorted({r.email for r in _unjournaled_invitations(db)}):
        lock_email(db, email)
    list(db.scalars(select(PasswordResetToken.id).where(ended).order_by(PasswordResetToken.id).with_for_update()))
    list(db.scalars(select(UserSession.id).where(UserSession.revoked_at.is_not(None))
                    .order_by(UserSession.id).with_for_update()))
    invitation_ids = [r.id for r in _unjournaled_invitations(db)]
    if db.scalar(text("SELECT to_regprocedure('ras_auth_invitation_lock(uuid[])') IS NOT NULL")):
        db.execute(text("SELECT ras_auth_invitation_lock(:ids)"), {"ids": invitation_ids})
    else:
        list(db.scalars(select(Invitation.id).where(Invitation.id.in_(invitation_ids))
                        .order_by(Invitation.id).with_for_update()))


def _imported_closure_body(rr: ReviewRequest) -> dict:
    """A close found in the database as a record; its code names the decision (also for ``closed_by_recovery``)."""
    return closure_body(rr) | {"status": closed_status_of(rr.close_reason_code)}


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
        if (db.scalar(select(func.count()).select_from(User)) or
                db.scalar(select(func.count()).select_from(LibraryItem)) or
                db.scalar(select(func.count()).select_from(UserSession)) or
                db.scalar(select(func.count()).select_from(PasswordResetToken)) or
                db.scalar(select(func.count()).select_from(Invitation)) or
                db.scalar(select(func.count()).select_from(DeletionTombstone)) or _db_head(db) is not None):
            raise RecoveryError("database_not_new")
        if path.exists():
            raise RecoveryError("journal_exists")
        record = journal.genesis(uuid.uuid4(), now, "new_install")
        db.add(RecoveryRegistration(id=1, journal_id=uuid.UUID(record.body["journal_id"]), genesis_hash=record.hash,
                                    mode="new_install", registered_at=now))
        db.add(_mirror(record, "genesis", "new_install", None, confirmed=True))  # create fsyncs before commit
        db.flush()
        try:
            journal.create(path, [record])
        except JournalError as exc:
            raise RecoveryError(exc.code) from None
        db.commit()
        return record


def register_existing(engine: Engine, settings: Settings, now: datetime) -> tuple[Record, int]:
    """Register the **current, live** database (the operator confirms it is not a restored copy).

    Writes a new journal with every existing tombstone and every closed review
    request (M6c-2b1), so a later restore of an older snapshot replays them too.
    Returns the genesis and the count imported.
    """
    path = _path(settings)
    with Session(engine) as db:
        _lock_legacy_sessions(db)
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
        db.add(_mirror(records[0], "genesis", "existing_db", None, confirmed=False))
        for tombstone in _unjournaled(db):
            records.append(journal.next_record(records[-1], REVOCATION, _body_of(db, tombstone)))
            db.add(_mirror(records[-1], "import", "imported", tombstone.id, confirmed=False))
        for rr in _unjournaled_closures(db):  # M6c-2b1: closed grants too, so no restore reopens them
            records.append(journal.next_record(records[-1], REVIEW_CLOSURE, _imported_closure_body(rr)))
            db.add(_mirror(records[-1], "import", "imported", None, confirmed=False, review_request_id=rr.id))
        for row in _unjournaled_invitations(db):
            records.append(journal.next_record(records[-1], INVITATION_CLOSURE, invitation_body(row)))
            db.add(_mirror(records[-1], "import", "imported", None, confirmed=False))
        for row in _unjournaled_reset_tokens(db):
            records.append(journal.next_record(records[-1], RESET_TOKEN_CLOSURE, reset_token_body(row)))
            db.add(_mirror(records[-1], "import", "imported", None, confirmed=False))
        for row in _unjournaled_sessions(db):
            records.append(journal.next_record(records[-1], SESSION_REVOCATION, session_body(row)))
            db.add(_mirror(records[-1], "import", "imported", None, confirmed=False,
                           session_id=row.id, session_user_id=row.user_id))
        db.flush()
        try:
            journal.create(path, records)
        except JournalError as exc:
            raise RecoveryError(exc.code) from None
        db.execute(update(RevocationJournalRecord).values(journal_confirmed=True))
        db.commit()
        return records[0], len(records) - 1


# --- recover ------------------------------------------------------------------

@dataclass
class RecoveryReport:
    adopted: bool = False
    journal_rebuilt: int = 0
    journal_repaired: int = 0
    journal_confirmed: int = 0
    closures_imported: int = 0
    sessions_imported: int = 0
    reset_tokens_imported: int = 0
    invitations_imported: int = 0
    torn_tail_dropped: bool = False
    replayed: dict[str, int] = field(default_factory=dict)
    cleanup: dict[str, int] = field(default_factory=dict)
    objects_removed: int = 0
    gate: str = "closed"
    reconciliation_complete: bool = False
    safety_ready: bool = False


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
        _lock_legacy_sessions(db)
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
        # M6c-2b1: review requests closed before 0028 have no record anywhere. A close is final, so a
        # record of one only ends a grant that really ended: no confirmation needed, whichever database
        # this is. Requests the file already names are left to the replay below.
        in_file = {r.body["request_id"] for r in state.records if r.kind == REVIEW_CLOSURE}
        closures = [rr for rr in _unjournaled_closures(db) if str(rr.id) not in in_file]
        if closures:
            added = []
            for rr in closures:
                added.append(journal.next_record(added[-1] if added else state.head, REVIEW_CLOSURE,
                                                 _imported_closure_body(rr)))
            try:
                state = journal.append(path, state, added)
            except JournalError as exc:
                raise RecoveryError(exc.code) from None
            except OSError:
                raise RecoveryError("journal_write_failed") from None
            report.closures_imported += len(added)
        in_file = {r.body["session_id"] for r in state.records if r.kind == SESSION_REVOCATION}
        sessions = [row for row in _unjournaled_sessions(db) if str(row.id) not in in_file]
        if sessions:
            added = []
            for row in sessions:
                added.append(journal.next_record(added[-1] if added else state.head, SESSION_REVOCATION,
                                                 session_body(row)))
            try:
                state = journal.append(path, state, added)
            except JournalError as exc:
                raise RecoveryError(exc.code) from None
            except OSError:
                raise RecoveryError("journal_write_failed") from None
            report.sessions_imported += len(added)
        # Every DB record is now in the file and matches it. Sync the file ourselves: once this returns,
        # each complete line is durable, so records whose write was never confirmed (fsync raised) are
        # confirmed now, and the lines replayed afterwards are mirrored confirmed.
        try:
            journal.sync(path)
        except OSError:
            raise RecoveryError("journal_sync_failed") from None
        report.journal_confirmed = db.execute(
            update(RevocationJournalRecord).where(RevocationJournalRecord.journal_confirmed.is_(False),
                                                  RevocationJournalRecord.seq <= state.head.seq)
            .values(journal_confirmed=True)).rowcount
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
    for record in state.records:
        result, _ = replay_record(engine, record, now)
        report.replayed[result] = report.replayed.get(result, 0) + 1


def _import_legacy_auth_tokens(engine: Engine, path: Path, report: RecoveryReport) -> None:
    # File decisions have already been replayed: mirror seqs stay contiguous.
    # Writers are stopped, and all auth row locks precede the journal lock.
    with Session(engine) as db:
        _lock_legacy_sessions(db)
        tokens = _unjournaled_reset_tokens(db)
        invitations = _unjournaled_invitations(db)
        batch = record_auth_endings(db, path, tokens, [], invitations=invitations, during_recovery=True)
        db.commit()
        report.reset_tokens_imported += len(tokens)
        report.invitations_imported += len(invitations)
        if any(not line.written for line in batch):
            raise RecoveryError("journal_write_failed")


def _reconcile_auth(engine: Engine, path: Path) -> None:
    """Phase 2 only after ALL events have been mirrored/folded. Writers stopped."""
    from app.auth_safety import verify_snapshot
    with Session(engine) as db:
        verify_snapshot(db)
        ids = list(db.scalars(select(User.id).order_by(User.id)))
    for uid in ids:
        with Session(engine) as db:
            db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
            _lock_journal(db)
            db.execute(text("SELECT ras_auth_restrict(:uid)"), {"uid": uid})
            db.commit()  # Release journal before the next User lock.


def _backfill_auth_safety(engine: Engine, path: Path, now: datetime, report: RecoveryReport) -> None:
    """No permissive baseline: only legacy disabled and legacy token/session closure."""
    from app.auth_safety import fresh_ids
    from app.models.enums import SessionRevokeReason
    with Session(engine) as db:
        ids = list(db.scalars(select(User.id).order_by(User.id)))
    for uid in ids:
        with Session(engine) as db:
            db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
            u = db.scalar(select(User).where(User.id == uid)
                          .execution_options(populate_existing=True))
            _lock_journal(db)
            state = journal.read(path)
            if u.status != "disabled" or any(r.kind == journal.ACCOUNT_STATUS_DECISION and r.body["user_id"] == str(uid) for r in state.records):
                db.rollback()
                continue
            op, generation = fresh_ids(state.records)
            record = journal.next_record(state.head, journal.ACCOUNT_STATUS_DECISION,
                {"user_id": str(uid), "operation_id": str(op), "generation": str(generation),
                 "decision": "disabled", "reason_code": "legacy_disabled", "recorded_at": now.isoformat()})
            db.add(_mirror(record, "import", "legacy_disabled", None, confirmed=False))
            db.flush()
            db.execute(text("SELECT ras_auth_project(:uid)"), {"uid": uid})
            db.execute(text("SELECT ras_auth_restrict(:uid)"), {"uid": uid})
            written = False
            try:
                journal.append(path, state, [record])
                written = True
            except (OSError, JournalError):
                pass
            if written:
                db.execute(text("SELECT ras_auth_confirm(:seq)"), {"seq": record.seq})
            db.commit()
            if not written:
                raise RecoveryError("journal_write_failed")
    # Separate batch: lock every User, then tokens, then sessions, before journal.
    with Session(engine) as db:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": list(db.scalars(select(User.id).order_by(User.id)))})
        tokens = list(db.scalars(select(PasswordResetToken).where(
            PasswordResetToken.credential_fence_operation_id.is_(None), PasswordResetToken.used_at.is_(None),
            PasswordResetToken.revoked_at.is_(None)).order_by(PasswordResetToken.id).with_for_update()))
        sessions = list(db.scalars(select(UserSession).where(UserSession.credential_generation.is_(None),
            UserSession.revoked_at.is_(None)).order_by(UserSession.id).with_for_update()))
        for t in tokens:
            t.revoked_at, t.closure_reason = now, "auth_safety_backfill"
        for s in sessions:
            s.revoked_at, s.revoked_reason = now, SessionRevokeReason.AUTH_SAFETY_BACKFILL.value
        db.flush()
        batch = record_auth_endings(db, path, tokens, sessions, during_recovery=True)
        db.commit()
        report.reset_tokens_imported += len(tokens)
        report.sessions_imported += len(sessions)
        if any(not line.written for line in batch):
            raise RecoveryError("journal_write_failed")


def _revocations(path: Path) -> list[Record]:
    return [r for r in journal.read(path).records if r.kind == REVOCATION]


def _closures(path: Path) -> list[Record]:
    return [r for r in journal.read(path).records if r.kind == REVIEW_CLOSURE]


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
        if _unconfirmed(db):
            raise RecoveryError("journal_unconfirmed")
        if _unjournaled_closures(db):
            raise RecoveryError("review_closures_not_journaled")
        if _unjournaled_sessions(db):
            raise RecoveryError("sessions_not_journaled")
        if _unjournaled_reset_tokens(db):
            raise RecoveryError("reset_tokens_not_journaled")
        if _unjournaled_invitations(db):
            raise RecoveryError("invitations_not_journaled")
        for record in journal.read(path).records:
            if record.kind == INVITATION_CLOSURE:
                _apply_invitation_closure(db.get(Invitation, uuid.UUID(record.body["invitation_id"]),
                                                options=_invitation_recovery_columns()),
                                          record.body, apply=False)
            if record.kind == RESET_TOKEN_CLOSURE:
                _apply_reset_token_closure(db.get(PasswordResetToken, uuid.UUID(record.body["token_id"])),
                                          record.body, apply=False)
            if record.kind != SESSION_REVOCATION:
                continue
            row = db.get(UserSession, uuid.UUID(record.body["session_id"]))
            if row is not None:
                if str(row.user_id) != record.body["user_id"]:
                    raise RecoveryError("record_names_another_session_user")
                if (row.revoked_at, row.revoked_reason) != (
                        datetime.fromisoformat(record.body["revoked_at"]), record.body["reason_code"]):
                    raise RecoveryError("session_not_revoked")
        for record in _closures(path):
            request_status = db.scalar(select(ReviewRequest.status).where(
                ReviewRequest.id == uuid.UUID(record.body["request_id"])))
            if request_status == ReviewStatus.OPEN.value:
                raise RecoveryError("review_request_still_open")
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


def hold(engine: Engine, settings: Settings, now: datetime) -> str:
    """Open a recovery right after a restore, so the gate stays shut until ``recover`` closes it (M6c-2a).

    Without it, a database restored from a backup taken after the last revocation would
    match the journal and open the gate before anyone ran ``recover``. Appends a
    ``recovery_begin`` to the journal only (``recover`` mirrors it and continues this same
    recovery); returns its ID, or the ID of a recovery already open. Refuses, writing nothing,
    when the journal cannot be read, has a torn tail, is not this database's, or lacks a
    record the database holds: appending then would turn a repairable journal into a
    diverged one. The gate is shut in every refused case too.
    """
    path = _path(settings)
    with engine.connect() as mutex:
        if not mutex.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": RECOVER_LOCK}):
            raise RecoveryError("recovery_already_running")
        mutex.commit()
        try:
            with Session(engine) as db:
                _lock_journal(db)
                try:
                    state = journal.read(path)
                except JournalError as exc:
                    raise RecoveryError(exc.code) from None
                if state.torn is not None:
                    raise RecoveryError("journal_torn")
                registration = _registration(db)
                if registration is not None and (registration.journal_id != state.journal_id
                                                 or registration.genesis_hash != state.genesis_hash):
                    raise RecoveryError("journal_mismatch")
                head = _db_head(db)
                if head is not None:
                    if head.seq > state.head.seq:
                        raise RecoveryError("journal_behind_db")
                    if state.records[head.seq - 1].hash != head.record_hash:
                        raise RecoveryError("journal_diverged")
                if state.open_recovery is not None:
                    return state.open_recovery
                recovery_id = str(uuid.uuid4())
                record = journal.next_record(state.head, RECOVERY_BEGIN, {
                    "recovery_id": recovery_id, "started_at": now.isoformat(), "db_head_seq": head.seq if head else 0})
                try:
                    journal.append(path, state, [record])
                except JournalError as exc:
                    raise RecoveryError(exc.code) from None
                except OSError:
                    raise RecoveryError("journal_write_failed") from None
                db.commit()
                return recovery_id
        finally:
            mutex.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": RECOVER_LOCK})
            mutex.commit()


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
            _reconcile_auth(engine, path)
            _backfill_auth_safety(engine, path, now, report)
            _import_legacy_auth_tokens(engine, path, report)
            _cleanup(engine, storage, settings, now, report)
            _sweep_objects(engine, storage, path, report)
            _verify(engine, path)
            from app.auth_safety import verify
            with Session(engine) as db:
                verify(db, journal.read(path).records)
            purged = sum(v for k, v in report.cleanup.items() if not k.startswith("error:"))
            _append_marker(engine, path, RECOVERY_END, {
                "recovery_id": recovery_id, "finished_at": now.isoformat(),
                "replayed": sum(v for k, v in report.replayed.items() if k not in ("present", "tombstone_present")),
                "tombstones_purged": purged, "objects_removed": report.objects_removed})
            _replay_all(engine, now, path, report)
            with Session(engine) as db:
                _lock_journal(db)
                db.execute(text("SELECT ras_auth_progress(:seq)"), {"seq": journal.read(path).head.seq})
                db.commit()
            report.reconciliation_complete = True
        finally:
            mutex.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": RECOVER_LOCK})
            mutex.commit()
    status = evaluate(settings, engine)
    report.gate = status.reason
    if not status.open and status.reason != "auth_safety_pending":
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
            out["unconfirmed_records"] = _unconfirmed(db)
            out["unjournaled_review_closures"] = len(_unjournaled_closures(db))
            out["unjournaled_sessions"] = len(_unjournaled_sessions(db))
            out["unjournaled_invitations"] = len(_unjournaled_invitations(db))
            out["unjournaled_reset_tokens"] = len(_unjournaled_reset_tokens(db))
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
