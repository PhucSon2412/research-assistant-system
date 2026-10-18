"""C2d-5e1 runtime cleanup of revoked common copies (Celery, CLI and direct).

The authority is a valid, journal-confirmed common-copy tombstone, never an
ID/key handed in by a caller or a cookie. Every checkpoint is a short Session
with its own runtime/gate entry, binding recheck after the journal lock and a
fresh clock; the object store is reached only between checkpoints, with no
DB Session, connection, row, key or journal lock held by the attempt.

1. **candidates**: bounded batch of unpurged tombstones, ``purge_due_at`` then
   ID (the deadline is when cleanup is due, not when it may start). A hint only.
2. **prepare**: per-key advisory lock (inside ``ras_auth_cleanup_fence``) and
   scalar reads; NO item/job/tombstone/resource lock, so it cannot close a
   key -> item cycle with a SourceFile writer (item -> key). A protected
   reference (private file, live/pending copy, file without/absent item)
   gives ``shared_kept`` and no delete authority. Otherwise the routine writes
   the permanent fence; flush/constraints -> journal exclusive -> exact
   rebinding -> fresh clock -> known commit + clean release. Uncertain commit
   or release failure stops here: no S3 in this attempt.
3. **pre_delete**: new transaction, journal shared, exact fence/tombstone/
   target/reference proof, fresh clock, clean release.
4. **S3** HEAD/delete/HEAD on the authorised key only. Only a real missing
   object is absence; 403/timeout/5xx are ``storage_unavailable``. Two workers
   may both delete (at least once); no PUT/copy/recreate ever.
5. **finalize**: tombstone (SKIP LOCKED) -> item -> derived/job/call/
   membership/metadata DML (implicit row locks) -> flush/constraints ->
   journal exclusive -> postconditions -> fresh clock -> known commit. Never
   the key lock (the fence is durable and immutable), never S3, never a second
   connection.

A known business refusal with clean rollback/release may write an error
checkpoint in a new transaction (IDs/codes only). An uncertain commit, a
release fault or a closed gate stops the batch: no inference, retry or error
writer in that attempt; a later independent tick reads the durable fence and
resumes. A delete admitted before the gate closed may still complete; it is
never undone, and finalisation then waits for an open gate.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, publication as rows
from app.audit import record_event
from app.config import get_settings
from app.ingest import runtime
from app.models import CollectionItem, DeletionTombstone, LibraryItem, ObjectDeleteFence, RevocationJournalRecord
from app.models.enums import AuditActorType, AuditOutcome, CleanupObjectOutcome, PublicationState, TombstoneKind

FENCE_ROUTINE = 'ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)'
TOMBSTONE = ('id', 'target_kind', 'item_id', 'version_id', 'source_file_id', 'storage_key', 'access_rights_id',
             'reason_code', 'prior_publication_state', 'rights_epoch', 'recorded_at', 'recorded_by_user_id',
             'purge_due_at')
LIVE_REFERENCES = text("""SELECT count(*) FROM source_files f LEFT JOIN library_items i ON i.id = f.item_id
    WHERE f.storage_key = :k AND (i.id IS NULL OR
    (i.publication_state IS DISTINCT FROM 'revoked' AND i.deleted_at IS NULL))""")


@dataclass(frozen=True)
class Proof:
    """Detached, immutable: what a committed/fresh checkpoint established."""
    tombstone_id: uuid.UUID
    binding: tuple = field(repr=False)
    kind: str  # 'delete' | 'shared_kept' | 'no_object'
    key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Refused:
    """A known business refusal; the transaction was rolled back, nothing committed."""
    code: str


def _now():
    from app.ingest import worker
    return worker.utcnow()


def _settings():
    from app.ingest import worker
    return worker.get_settings()


def _session(engine) -> Session:
    return Session(engine, expire_on_commit=False)


def entry(db) -> None:
    """Actual runtime login with the fence routine, then the writer gate, in its own released transaction."""
    def check(_):
        runtime.require_runtime(db)
        try:
            granted = db.scalar(text(f"SELECT has_function_privilege('{FENCE_ROUTINE}','EXECUTE')"))
        except Exception:
            raise runtime.Halt('worker_runtime_unavailable') from None
        if granted is not True:
            raise runtime.Halt('worker_runtime_required')
        try:
            auth_safety.require_writer(db)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
    runtime.short(db, check)


def _timeout(db) -> None:
    db.execute(text(f'SET LOCAL lock_timeout = {int(_settings().recovery.gate_lock_timeout_ms)}'))


def _final_gate(db, *, writer: bool) -> None:
    try:
        auth_safety.require_final(db, writer=writer)
    except Exception:
        raise runtime.Halt('recovery_gate_closed') from None


def _sqlstate(exc) -> str | None:
    return getattr(getattr(exc, 'orig', None), 'sqlstate', None)


def _tomb(db, tombstone_id):
    return db.execute(select(*(getattr(DeletionTombstone, c) for c in TOMBSTONE), DeletionTombstone.purged_at)
                      .where(DeletionTombstone.id == tombstone_id)).one_or_none()


def _binding(row) -> tuple:
    return tuple(getattr(row, c) for c in TOMBSTONE)


def _confirmed(db, tombstone_id) -> bool:
    return bool(db.scalar(select(func.count()).select_from(RevocationJournalRecord).where(
        RevocationJournalRecord.tombstone_id == tombstone_id, RevocationJournalRecord.kind == 'revocation',
        RevocationJournalRecord.journal_confirmed.is_(True))))


def _target(db, item_id):
    return db.execute(select(LibraryItem.visibility, LibraryItem.publication_state, LibraryItem.rights_epoch)
                      .where(LibraryItem.id == item_id)).one_or_none()


def _target_ok(target, tomb) -> bool:
    # Absent target: allowed only through the confirmed tombstone (restored/older snapshot).
    return target is None or (target.visibility == 'common'
                              and target.publication_state == PublicationState.REVOKED.value
                              and target.rights_epoch == tomb.rights_epoch)


def _fence(db, key):
    return db.execute(select(ObjectDeleteFence.storage_key, ObjectDeleteFence.tombstone_id, ObjectDeleteFence.item_id,
                             ObjectDeleteFence.source_file_id, ObjectDeleteFence.rights_epoch)
                      .where(ObjectDeleteFence.storage_key == key)).one_or_none()


def _live(db, key) -> int:
    return db.scalar(LIVE_REFERENCES, {'k': key})


def _fresh_clock(floor):
    now = _now()  # after the LAST SQL predicate and any lock/journal wait
    if now < floor:
        raise runtime.Halt('cleanup_clock_invalid')
    return now


def candidates(engine, batch_size: int) -> tuple:
    db = _session(engine)
    entry(db)
    def read(_):
        return tuple(db.scalars(select(DeletionTombstone.id).where(
            DeletionTombstone.purged_at.is_(None), DeletionTombstone.target_kind == TombstoneKind.COMMON_COPY.value)
            .order_by(DeletionTombstone.purge_due_at, DeletionTombstone.id).limit(batch_size)))
    return runtime.short(db, read)


def prepare(engine, tombstone_id):
    """Fence (or recognise) the key under its advisory lock; known commit before any S3."""
    db = _session(engine)
    entry(db)
    def write(outcome):
        db.info['cleanup_checkpoint'] = 'prepare'
        _timeout(db)
        tomb = _tomb(db, tombstone_id)
        if tomb is None or tomb.purged_at is not None:
            return None
        if tomb.target_kind != TombstoneKind.COMMON_COPY.value or not _confirmed(db, tomb.id):
            raise runtime.Halt('cleanup_tombstone_unconfirmed')
        binding = _binding(tomb)
        target = _target(db, tomb.item_id)
        if not _target_ok(target, tomb):
            return Refused('target_not_revoked')
        if target is not None and rows._derived_shared(db, tomb.item_id):
            return Refused('derived_rows_shared')  # before any fence/S3; finalize rechecks under its locks
        fenced = False
        if tomb.storage_key is None:
            kind = 'no_object'
        else:
            try:
                result = db.scalar(text('SELECT ras_auth_cleanup_fence(:t, :i, :f, :k, :e)'), dict(
                    t=tomb.id, i=tomb.item_id, f=tomb.source_file_id, k=tomb.storage_key, e=tomb.rights_epoch))
            except DBAPIError as exc:
                if _sqlstate(exc) == '23514':  # the routine's own refusal: binding moved under the key lock
                    return Refused('cleanup_binding_changed')
                if _sqlstate(exc) == '55P03':
                    return Refused('cleanup_busy')
                raise
            if result not in ('fenced', 'already_fenced', 'shared_kept'):
                raise runtime.Halt('cleanup_fence_unavailable')
            kind = 'shared_kept' if result == 'shared_kept' else 'delete'
            fenced = result == 'fenced'
        if fenced:
            db.flush()
            db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        _final_gate(db, writer=fenced)
        fresh = _tomb(db, tombstone_id)
        if fresh is None or fresh.purged_at is not None or _binding(fresh) != binding or not _confirmed(db, fresh.id):
            raise runtime.Halt('cleanup_binding_changed')
        if not _target_ok(_target(db, fresh.item_id), fresh):
            raise runtime.Halt('cleanup_binding_changed')
        if kind == 'delete':
            fence = _fence(db, fresh.storage_key)
            if fence is None or (fenced and tuple(fence) != (fresh.storage_key, fresh.id, fresh.item_id,
                                                              fresh.source_file_id, fresh.rights_epoch)):
                raise runtime.Halt('cleanup_fence_unavailable')
            if _live(db, fresh.storage_key):
                raise runtime.Halt('cleanup_reference_conflict')
        elif kind == 'shared_kept' and not _live(db, fresh.storage_key):
            raise runtime.Halt('cleanup_binding_changed')
        _fresh_clock(fresh.recorded_at)
        proof = Proof(fresh.id, binding, kind, fresh.storage_key)
        if fenced:
            runtime.commit(db, outcome)
        return proof
    return runtime.short(db, write)


def pre_delete(engine, proof: Proof) -> None:
    """A NEW proof after the fence commit; the Session is released before S3."""
    db = _session(engine)
    entry(db)
    def read(_):
        db.info['cleanup_checkpoint'] = 'pre_delete'
        _timeout(db)
        _final_gate(db, writer=False)
        tomb = _tomb(db, proof.tombstone_id)
        if (tomb is None or tomb.purged_at is not None or _binding(tomb) != proof.binding
                or not _confirmed(db, tomb.id) or tomb.storage_key != proof.key or proof.kind != 'delete'):
            raise runtime.Halt('cleanup_binding_changed')
        if not _target_ok(_target(db, tomb.item_id), tomb):
            raise runtime.Halt('cleanup_binding_changed')
        if _fence(db, proof.key) is None:
            raise runtime.Halt('cleanup_fence_unavailable')
        if _live(db, proof.key):
            raise runtime.Halt('cleanup_reference_conflict')
        _fresh_clock(tomb.recorded_at)
    runtime.short(db, read)


def finalize(engine, proof: Proof, object_outcome: CleanupObjectOutcome):
    """Content decision: tombstone -> item -> derived rows -> journal; purged_at only on proof."""
    db = _session(engine)
    entry(db)
    def write(outcome):
        db.info['cleanup_checkpoint'] = 'finalize'
        _timeout(db)
        try:
            tomb = db.scalar(select(DeletionTombstone).where(DeletionTombstone.id == proof.tombstone_id)
                             .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if tomb is None or tomb.purged_at is not None:
                return None  # another cleanup holds or finished it: exactly one success
            if _binding(tomb) != proof.binding or not _confirmed(db, tomb.id):
                raise runtime.Halt('cleanup_binding_changed')
            # Tombstone, then item: revoke/replay lock the item and only insert tombstones.
            item = db.scalar(select(LibraryItem).where(LibraryItem.id == tomb.item_id).with_for_update()
                             .execution_options(populate_existing=True))
        except DBAPIError as exc:
            if _sqlstate(exc) == '55P03':
                return Refused('cleanup_busy')
            raise
        if item is not None and not _target_ok(item, tomb):
            return Refused('target_not_revoked')
        if proof.kind == 'delete':
            if object_outcome not in (CleanupObjectOutcome.DELETED, CleanupObjectOutcome.ALREADY_ABSENT):
                raise runtime.Halt('cleanup_binding_changed')
            if _fence(db, proof.key) is None:
                raise runtime.Halt('cleanup_fence_unavailable')
            if _live(db, proof.key):
                raise runtime.Halt('cleanup_reference_conflict')
        elif proof.kind == 'shared_kept':
            if object_outcome is not CleanupObjectOutcome.SHARED_KEPT or _fence(db, proof.key) is not None:
                raise runtime.Halt('cleanup_binding_changed')
            if not _live(db, proof.key):
                return Refused('shared_reference_changed')  # retried: a fresh prepare may fence it
        elif tomb.storage_key is not None or object_outcome is not CleanupObjectOutcome.NO_OBJECT:
            raise runtime.Halt('cleanup_binding_changed')
        at = _now()
        try:
            # From t_rev the DB refuses new derived rows (0024): every row to remove is committed.
            derived, scrubbed = rows._remove_derived(db, tomb.item_id, at) if item is not None else ({}, 0)
        except rows.CleanupError as exc:
            return Refused(exc.code)
        jobs = rows._finish_jobs(db, tomb.item_id, at) if item is not None else 0
        memberships = db.execute(delete(CollectionItem).where(CollectionItem.item_id == tomb.item_id)
                                 .execution_options(synchronize_session=False)).rowcount
        scrubbed_fields, kept = rows._remove_metadata(db, tomb.item_id, at) if item is not None else ([], [])
        attempts = tomb.cleanup_attempts + 1
        tomb.scrubbed_fields, tomb.retained_fields = scrubbed_fields, kept
        tomb.purged_at, tomb.object_outcome = at, object_outcome.value
        tomb.cleanup_attempts, tomb.last_cleanup_at, tomb.last_cleanup_error = attempts, at, None
        record_event(db, action='common_copy.cleanup', outcome=AuditOutcome.SUCCESS,
                     actor_type=AuditActorType.SYSTEM, target_type='deletion_tombstone', target_id=str(tomb.id),
                     details={'object_outcome': object_outcome.value, 'memberships_removed': memberships,
                              'derived_rows_removed': derived, 'embedding_calls_scrubbed': scrubbed,
                              'jobs_cancelled': jobs, 'scrubbed_fields': scrubbed_fields, 'retained_fields': kept})
        try:
            db.flush()
            db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        except DBAPIError as exc:
            if (_sqlstate(exc) or '').startswith('23'):  # 0023/0024 purge guards: rows or fields left
                return Refused('cleanup_failed')
            raise
        _final_gate(db, writer=True)
        with db.no_autoflush:
            state = db.execute(select(*(getattr(DeletionTombstone, c) for c in TOMBSTONE), DeletionTombstone.purged_at,
                                      DeletionTombstone.object_outcome, DeletionTombstone.cleanup_attempts)
                               .where(DeletionTombstone.id == tomb.id)).one()
            if (_binding(state) != proof.binding or state.purged_at != at
                    or state.object_outcome != object_outcome.value or state.cleanup_attempts != attempts):
                raise runtime.Halt('cleanup_postcondition_failed')
            if item is not None:
                if not _target_ok(_target(db, tomb.item_id), tomb):
                    raise runtime.Halt('cleanup_postcondition_failed')
                if db.scalar(text('SELECT ras_common_copy_derived_left(:i)'), {'i': tomb.item_id}):
                    raise runtime.Halt('cleanup_postcondition_failed')
            if proof.kind == 'delete' and (_fence(db, proof.key) is None or _live(db, proof.key)):
                raise runtime.Halt('cleanup_postcondition_failed')
            _fresh_clock(at)
        runtime.commit(db, outcome)
        return object_outcome.value
    return runtime.short(db, write)


def record_error(engine, tombstone_id, binding, code: str) -> bool:
    """Error checkpoint after a KNOWN refusal with clean rollback/release; codes only."""
    db = _session(engine)
    entry(db)
    def write(outcome):
        db.info['cleanup_checkpoint'] = 'error'
        _timeout(db)
        try:
            tomb = db.scalar(select(DeletionTombstone).where(DeletionTombstone.id == tombstone_id)
                             .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        except DBAPIError as exc:
            if _sqlstate(exc) == '55P03':
                return False
            raise
        if tomb is None or tomb.purged_at is not None:
            return False
        if binding is not None and _binding(tomb) != binding:
            raise runtime.Halt('cleanup_binding_changed')
        at, attempts = _now(), tomb.cleanup_attempts + 1
        tomb.cleanup_attempts, tomb.last_cleanup_at, tomb.last_cleanup_error = attempts, at, code
        db.flush()
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        _final_gate(db, writer=True)
        state = _tomb(db, tombstone_id)
        current = db.execute(select(DeletionTombstone.cleanup_attempts, DeletionTombstone.last_cleanup_error)
                             .where(DeletionTombstone.id == tombstone_id)).one()
        if state.purged_at is not None or tuple(current) != (attempts, code) or (
                binding is not None and _binding(state) != binding):
            raise runtime.Halt('cleanup_postcondition_failed')
        _fresh_clock(at)
        runtime.commit(db, outcome)
        return True
    return runtime.short(db, write)


def _one(engine, storage, tombstone_id) -> str | None:
    proof = prepare(engine, tombstone_id)
    if proof is None:
        return None
    if isinstance(proof, Refused):
        return _refused(engine, tombstone_id, None, proof.code)
    if proof.kind == 'delete':
        pre_delete(engine, proof)
        try:
            object_outcome = rows._remove_object(storage, proof.key)
        except rows.CleanupError as exc:
            return _refused(engine, tombstone_id, proof.binding, exc.code)
    else:
        object_outcome = (CleanupObjectOutcome.SHARED_KEPT if proof.kind == 'shared_kept'
                          else CleanupObjectOutcome.NO_OBJECT)
    done = finalize(engine, proof, object_outcome)
    if isinstance(done, Refused):
        return _refused(engine, tombstone_id, proof.binding, done.code)
    return done


def _refused(engine, tombstone_id, binding, code: str) -> str:
    if code == 'cleanup_busy':
        return 'skipped:cleanup_busy'  # lock contention: no attempt recorded, next tick retries
    record_error(engine, tombstone_id, binding, code)
    return 'error:' + code


def cleanup_revoked(engine, storage, settings=None) -> dict[str, int]:
    """One bounded runtime batch. Codes/counts only; a halt stops the batch without inference."""
    settings = settings or get_settings()
    counts: dict[str, int] = {}
    def add(key):
        counts[key] = counts.get(key, 0) + 1
    try:
        for tombstone_id in candidates(engine, settings.retention.cleanup_batch_size):
            result = _one(engine, storage, tombstone_id)
            if result is not None:
                add(result)
    except runtime.Halt as halt:
        add('halted:' + halt.code)
    if not any(code.startswith('halted:') for code in counts):
        from app.private_cleanup import cleanup_private
        counts.update({'private:'+code:count for code,count in cleanup_private(engine,storage,settings).items()})
    return counts
