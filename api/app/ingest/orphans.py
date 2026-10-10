"""C2d-5e2 manual orphan original reconcile through the runtime (CLI and direct).

Operational precondition, NOT verified here: the environment is quiescent.
No upload/approval or other writer may still PUT/copy an original or commit a
SourceFile (every attempt, SDK/DB thread and remote transaction has settled).
The grace period, the fence and this boundary do not prove that; they only
make one key's deletion decision durable and refuse new references to it.

Authority is the database, never a caller: an original key under
``originals/`` of the configured bucket that NO ``source_files`` row names
(any item, visibility or state), older than ``files.orphan_grace``. Every
checkpoint is a short Session with its own runtime/gate entry, exact rebinding
after the journal lock and a clock read after its last SQL; storage is reached
only between checkpoints, with no Session, connection, row, key or journal
lock held by the attempt.

1. **entry** before any storage call, even for an empty bucket.
2. **resume**: fences without completion (fenced_at, ID), bounded; they need
   no listing.
3. **scan** (bounded pages from the cursor hint): list outside any Session,
   reference/fence hints in a short read, then per candidate a fresh HEAD
   outside the DB (list and HEAD must agree), a fresh clock and **prepare**:
   ``ras_auth_orphan_fence`` takes the 0044 key lock and checks zero
   references; flush/constraints -> journal exclusive -> exact fence/reference
   proof -> fresh clock -> known commit + clean release, else no storage.
4. **pre_delete**: new transaction, journal shared, exact fence, no completion,
   zero references, fresh clock, clean release.
5. **storage** HEAD (identity against the fence observation) -> DELETE -> HEAD.
   Only a real missing key is absence; 403/timeout/5xx halt the pass. At least
   once; never PUT/copy/recreate; no conditional delete is claimed.
6. **finalize**: ``ras_auth_orphan_complete`` (one completion per fence) +
   audit -> flush/constraints -> journal exclusive -> exact completion ->
   fresh clock -> known commit. Never the key lock, never storage.

An uncertain commit, release fault, closed gate, storage or clock fault halts
the pass: no inference, retry or next candidate. A later independent pass
resumes from the durable fence. A delete admitted before the gate closed may
complete; it is never undone and finalisation waits for an open gate.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety
from app.audit import record_event
from app.clock import utcnow
from app.ingest import runtime
from app.models import OrphanObjectCompletion, OrphanObjectFence, OrphanReconcileCursor, SourceFile
from app.models.enums import AuditActorType, AuditOutcome
from app.storage import ORIGINALS_PREFIX, is_missing_object

FENCE_ROUTINE = 'ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz)'
COMPLETE_ROUTINE = 'ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz)'
KEY = re.compile(r'originals/[0-9a-f]{32}')  # the only keys upload/approval mint
CURSOR_MAX_CHARS = 1024
OUTCOMES = ('deleted', 'already_absent', 'kept_identity_changed')
REFERENCES = text('SELECT count(*) FROM source_files WHERE storage_key = :k')


@dataclass(frozen=True)
class Proof:
    """Detached, immutable: the exact fence a committed/fresh checkpoint established."""
    fence_id: uuid.UUID
    key: str = field(repr=False)
    size: int
    last_modified: datetime
    fenced_at: datetime
    etag: str | None = field(default=None, repr=False)  # in-memory only; never stored


@dataclass(frozen=True)
class Refused:
    """A known non-mutation; the transaction was rolled back, nothing committed."""
    code: str


@dataclass(frozen=True)
class Seen:
    """One validated listing entry or HEAD observation (UTC, whole seconds for identity)."""
    key: str = field(repr=False)
    size: int
    last_modified: datetime
    etag: str | None = field(default=None, repr=False)


def _session(engine) -> Session:
    return Session(engine, expire_on_commit=False)


def _now() -> datetime:
    now = utcnow()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise runtime.Halt('orphan_clock_invalid')
    return now.astimezone(UTC)


class _Clock:
    """Monotonic floor across the pass: a clock behind an earlier reading halts."""

    def __init__(self):
        self.floor = None

    def fresh(self, floor=None) -> datetime:
        now = _now()
        for bound in (self.floor, floor):
            if bound is not None and now < bound:
                raise runtime.Halt('orphan_clock_invalid')
        self.floor = now
        return now


def _old(last_modified: datetime, now: datetime, grace: timedelta) -> bool:
    """Eligible only strictly before the cutoff: ``last_modified < now - grace``."""
    return last_modified < now - grace


def _instant(value) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _seen(key, size, last_modified, etag) -> Seen | None:
    """Storage metadata or None when unusable (never deleted on doubt)."""
    at = _instant(last_modified)
    if (not isinstance(key, str) or not KEY.fullmatch(key) or at is None or isinstance(size, bool)
            or not isinstance(size, int) or size < 0 or (etag is not None and not isinstance(etag, str))):
        return None
    return Seen(key, size, at.replace(microsecond=0), etag)


def _same(a: Seen, b: Seen) -> bool:
    # Listing reports sub-seconds where HEAD reports whole seconds; both are compared at seconds.
    return (a.size, a.last_modified) == (b.size, b.last_modified) and (
        a.etag is None or b.etag is None or a.etag == b.etag)


def entry(db) -> None:
    """Actual runtime login with both routines, then the writer gate, in its own released transaction."""
    def check(_):
        runtime.require_runtime(db)
        try:
            granted = db.scalar(text(f"SELECT has_function_privilege('{FENCE_ROUTINE}','EXECUTE')"
                                     f" AND has_function_privilege('{COMPLETE_ROUTINE}','EXECUTE')"))
        except Exception:
            raise runtime.Halt('worker_runtime_unavailable') from None
        if granted is not True:
            raise runtime.Halt('worker_runtime_required')
        try:
            auth_safety.require_writer(db)
        except Exception:
            raise runtime.Halt('recovery_gate_closed') from None
    runtime.short(db, check)


def _timeout(db, settings) -> None:
    db.execute(text(f'SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}'))


def _final_gate(db, *, writer: bool) -> None:
    try:
        auth_safety.require_final(db, writer=writer)
    except Exception:
        raise runtime.Halt('recovery_gate_closed') from None


def _sqlstate(exc) -> str | None:
    return getattr(getattr(exc, 'orig', None), 'sqlstate', None)


def _fence(db, key):
    return db.execute(select(OrphanObjectFence.id, OrphanObjectFence.storage_key, OrphanObjectFence.run_id,
                             OrphanObjectFence.observed_size, OrphanObjectFence.observed_last_modified,
                             OrphanObjectFence.grace_cutoff, OrphanObjectFence.fenced_at)
                      .where(OrphanObjectFence.storage_key == key)).one_or_none()


def _completion(db, fence_id):
    return db.execute(select(OrphanObjectCompletion.fence_id, OrphanObjectCompletion.outcome,
                             OrphanObjectCompletion.run_id, OrphanObjectCompletion.completed_at)
                      .where(OrphanObjectCompletion.fence_id == fence_id)).one_or_none()


def _references(db, key) -> int:
    return db.scalar(REFERENCES, {'k': key})


def _proof(row, etag=None) -> Proof:
    return Proof(row.id, row.storage_key, row.observed_size, row.observed_last_modified, row.fenced_at, etag)


def _matches(row, proof: Proof) -> bool:
    return row is not None and (row.id, row.storage_key, row.observed_size, row.observed_last_modified,
                                row.fenced_at) == (proof.fence_id, proof.key, proof.size, proof.last_modified,
                                                   proof.fenced_at)


def _read(engine, operation):
    db = _session(engine)
    entry(db)
    return runtime.short(db, lambda _: operation(db))


def pending(engine, limit: int) -> tuple[Proof, ...]:
    """Fences without completion, oldest first: resumed without any listing."""
    def read(db):
        rows = db.execute(select(OrphanObjectFence.id, OrphanObjectFence.storage_key, OrphanObjectFence.observed_size,
                                 OrphanObjectFence.observed_last_modified, OrphanObjectFence.fenced_at)
                          .outerjoin(OrphanObjectCompletion, OrphanObjectCompletion.fence_id == OrphanObjectFence.id)
                          .where(OrphanObjectCompletion.fence_id.is_(None))
                          .order_by(OrphanObjectFence.fenced_at, OrphanObjectFence.id).limit(limit)).all()
        return tuple(_proof(r) for r in rows)
    return _read(engine, read)


def hints(engine, keys) -> tuple[frozenset, frozenset]:
    """Referenced and fenced keys among one page: hints to skip early, never authority."""
    keys = sorted(keys)
    def read(db):
        if not keys:
            return frozenset(), frozenset()
        referenced = db.scalars(select(SourceFile.storage_key).where(SourceFile.storage_key.in_(keys))).all()
        fenced = db.scalars(select(OrphanObjectFence.storage_key).where(OrphanObjectFence.storage_key.in_(keys))).all()
        return frozenset(referenced), frozenset(fenced)
    return _read(engine, read)


def _valid_cursor(value) -> bool:
    return value is None or (isinstance(value, str) and value.startswith(ORIGINALS_PREFIX)
                             and len(value) <= CURSOR_MAX_CHARS and value.isprintable())


def read_cursor(engine) -> str | None:
    def read(db):
        value = db.scalar(select(OrphanReconcileCursor.start_after).where(OrphanReconcileCursor.id == 1))
        if not _valid_cursor(value):
            raise runtime.Halt('orphan_cursor_invalid')
        return value
    return _read(engine, read)


def write_cursor(engine, settings, clock: _Clock, value: str | None) -> None:
    """Progress hint only; through the writer gate like any mutation."""
    if not _valid_cursor(value):
        raise runtime.Halt('orphan_cursor_invalid')
    db = _session(engine)
    entry(db)
    def write(outcome):
        db.info['orphan_checkpoint'] = 'cursor'
        _timeout(db, settings)
        at = clock.fresh()
        db.execute(text("""INSERT INTO orphan_reconcile_cursors(id,start_after,updated_at) VALUES(1,:s,:t)
            ON CONFLICT (id) DO UPDATE SET start_after=EXCLUDED.start_after, updated_at=EXCLUDED.updated_at"""),
                   {'s': value, 't': at})
        db.flush()
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        _final_gate(db, writer=True)
        row = db.execute(select(OrphanReconcileCursor.start_after, OrphanReconcileCursor.updated_at)
                         .where(OrphanReconcileCursor.id == 1)).one_or_none()
        if row is None or tuple(row) != (value, at):
            raise runtime.Halt('orphan_postcondition_failed')
        clock.fresh(at)
        runtime.commit(db, outcome)
    runtime.short(db, write)


def prepare(engine, settings, clock: _Clock, seen: Seen, run: uuid.UUID):
    """Fence (or recognise) the key under its advisory lock; known commit before any storage."""
    grace = settings.files.orphan_grace
    db = _session(engine)
    entry(db)
    def write(outcome):
        db.info['orphan_checkpoint'] = 'prepare'
        _timeout(db, settings)
        at = clock.fresh()
        if not _old(seen.last_modified, at, grace):
            return Refused('recent')
        try:
            result = db.scalar(text('SELECT ras_auth_orphan_fence(:r,:k,:n,:m,:c,:t)'), dict(
                r=run, k=seen.key, n=seen.size, m=seen.last_modified, c=at - grace, t=at))
        except DBAPIError as exc:
            if _sqlstate(exc) == '55P03':
                return Refused('busy')
            if _sqlstate(exc) == '23514':  # the routine's own refusal (invalid binding, reference on a fence)
                raise runtime.Halt('orphan_fence_refused') from None
            raise
        if result == 'referenced':
            return Refused('referenced')
        if result not in ('fenced', 'already_fenced'):
            raise runtime.Halt('orphan_fence_unavailable')
        fenced = result == 'fenced'
        if fenced:
            db.flush()
            db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        _final_gate(db, writer=fenced)
        row = _fence(db, seen.key)
        if row is None or (fenced and (row.run_id, row.observed_size, row.observed_last_modified, row.grace_cutoff,
                                       row.fenced_at) != (run, seen.size, seen.last_modified, at - grace, at)):
            raise runtime.Halt('orphan_fence_unavailable')
        if _references(db, seen.key):
            raise runtime.Halt('orphan_reference_conflict')
        done = _completion(db, row.id) is not None
        clock.fresh(row.fenced_at if fenced else None)  # after the LAST SQL predicate and the journal wait
        if done:
            return Refused('already_completed')
        same = (row.observed_size, row.observed_last_modified) == (seen.size, seen.last_modified)
        proof = _proof(row, seen.etag if same else None)
        if fenced:
            runtime.commit(db, outcome)
        return proof
    return runtime.short(db, write)


def pre_delete(engine, settings, clock: _Clock, proof: Proof):
    """A NEW proof after the fence commit; the Session is released before storage."""
    db = _session(engine)
    entry(db)
    def read(_):
        db.info['orphan_checkpoint'] = 'pre_delete'
        _timeout(db, settings)
        _final_gate(db, writer=False)
        row = _fence(db, proof.key)
        if not _matches(row, proof):
            raise runtime.Halt('orphan_binding_changed')
        if _references(db, proof.key):
            raise runtime.Halt('orphan_reference_conflict')
        done = _completion(db, proof.fence_id) is not None
        now = clock.fresh(proof.fenced_at)
        if done:
            return Refused('already_completed')
        if not _old(proof.last_modified, now, settings.files.orphan_grace):
            return Refused('recent')  # only with a larger grace than at fence time; the fence stays
        return None
    return runtime.short(db, read)


def _head(storage, key: str) -> Seen | None:
    """Fresh HEAD outside the DB; None for a real missing key. Other faults halt."""
    try:
        size, etag, last_modified = storage.head(key)
    except Exception as exc:
        if is_missing_object(exc):
            return None
        raise runtime.Halt('storage_unavailable') from None
    seen = _seen(key, size, last_modified, etag)
    if seen is None:
        raise runtime.Halt('storage_metadata_invalid')
    return seen


def remove(storage, proof: Proof) -> str:
    """HEAD -> DELETE -> HEAD on the fenced key only, with no DB connection held."""
    current = _head(storage, proof.key)
    if current is None:
        return 'already_absent'
    if not _same(current, Seen(proof.key, proof.size, proof.last_modified, proof.etag)):
        return 'kept_identity_changed'  # rewritten after the fence observation: never deleted on old metadata
    try:
        storage.delete(proof.key)
    except Exception as exc:
        if not is_missing_object(exc):
            raise runtime.Halt('storage_unavailable') from None
    if _head(storage, proof.key) is None:
        return 'deleted'
    raise runtime.Halt('orphan_object_still_present')


def finalize(engine, settings, clock: _Clock, proof: Proof, object_outcome: str, run: uuid.UUID) -> str:
    """One completion per fence after storage verification; audit IDs/codes only."""
    if object_outcome not in OUTCOMES:
        raise runtime.Halt('orphan_binding_changed')
    db = _session(engine)
    entry(db)
    def write(outcome):
        db.info['orphan_checkpoint'] = 'finalize'
        _timeout(db, settings)
        at = clock.fresh(proof.fenced_at)
        try:
            result = db.scalar(text('SELECT ras_auth_orphan_complete(:f,:k,:r,:o,:t)'), dict(
                f=proof.fence_id, k=proof.key, r=run, o=object_outcome, t=at))
        except DBAPIError as exc:
            if _sqlstate(exc) == '23514':
                raise runtime.Halt('orphan_finalize_refused') from None
            raise
        if result == 'already_completed':
            clock.fresh(at)
            return 'already_completed'  # another pass completed it: exactly one completion/audit
        if result != 'completed':
            raise runtime.Halt('orphan_finalize_refused')
        record_event(db, action='original.orphan_reconcile', outcome=AuditOutcome.SUCCESS,
                     actor_type=AuditActorType.SYSTEM, target_type='orphan_object_fence',
                     target_id=str(proof.fence_id), details={'object_outcome': object_outcome, 'run_id': str(run)})
        db.flush()
        db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        _final_gate(db, writer=True)
        with db.no_autoflush:
            if not _matches(_fence(db, proof.key), proof) or _references(db, proof.key):
                raise runtime.Halt('orphan_postcondition_failed')
            done = _completion(db, proof.fence_id)
            if done is None or tuple(done) != (proof.fence_id, object_outcome, run, at):
                raise runtime.Halt('orphan_postcondition_failed')
            clock.fresh(at)
        runtime.commit(db, outcome)
        return object_outcome
    return runtime.short(db, write)


RESULT = {'deleted': 'deleted', 'already_absent': 'already_absent',
          'kept_identity_changed': 'kept:identity_changed', 'already_completed': 'already_completed'}


def _finish(engine, storage, settings, clock, proof: Proof, run) -> str:
    early = pre_delete(engine, settings, clock, proof)
    if isinstance(early, Refused):
        return RESULT.get(early.code, 'kept:' + early.code)
    object_outcome = remove(storage, proof)
    return RESULT[finalize(engine, settings, clock, proof, object_outcome, run)]


def _classify(item, referenced, fenced, now, grace) -> tuple[str, Seen | None]:
    if not isinstance(item.key, str) or not KEY.fullmatch(item.key):
        return 'kept:foreign_key', None  # not a key the application mints: never touched
    seen = _seen(item.key, item.size, item.last_modified, item.etag)
    if item.key in referenced:
        return 'kept:referenced', None
    if item.key in fenced:
        return 'kept:fenced', None  # resumed from its fence, not from the listing
    if seen is None:
        return 'kept:metadata_invalid', None
    if not _old(seen.last_modified, now, grace):  # whole seconds, as HEAD reports and the fence stores
        return 'kept:recent', None
    return 'candidate', seen


def _page(storage, settings, start_after):
    try:
        entries, truncated = storage.list_originals(start_after=start_after,
                                                    max_keys=settings.files.orphan_list_page_size)
    except Exception:
        raise runtime.Halt('storage_unavailable') from None
    if not isinstance(truncated, bool) or (truncated and not entries) or len(entries) > settings.files.orphan_list_page_size:
        raise runtime.Halt('storage_listing_invalid')
    previous = start_after
    for item in entries:  # strictly ascending after the cursor, or the listing cannot be trusted to end
        if not isinstance(item.key, str) or (previous is not None and item.key <= previous):
            raise runtime.Halt('storage_listing_invalid')
        previous = item.key
    return entries, truncated


def _candidate(engine, storage, settings, clock, listed: Seen, run) -> str:
    current = _head(storage, listed.key)
    if current is None:
        return 'skipped:vanished'
    if not _same(listed, current):
        return 'kept:changed'  # rewritten since the listing: the old metadata decides nothing
    if not _old(current.last_modified, clock.fresh(), settings.files.orphan_grace):
        return 'kept:recent'
    proof = prepare(engine, settings, clock, current, run)
    if isinstance(proof, Refused):
        return {'referenced': 'kept:referenced', 'recent': 'kept:recent', 'busy': 'skipped:busy',
                'already_completed': 'already_completed'}[proof.code]
    return _finish(engine, storage, settings, clock, proof, run)


def reconcile_originals(engine, storage, settings, *, apply: bool) -> dict[str, int]:
    """One bounded pass. Codes/counts only; a halt stops the pass without inference.

    ``apply=False`` is a preview: entry, pending count, bounded listing and
    hints; no HEAD, fence, cursor, audit or delete. ``apply=True`` is an
    operating choice, not proof of quiescence or a gate bypass.
    """
    counts: dict[str, int] = {}
    def add(key, n=1):
        counts[key] = counts.get(key, 0) + n
    files, run, clock = settings.files, uuid.uuid4(), _Clock()
    budget = files.orphan_max_deletes
    try:
        db = _session(engine)
        entry(db)  # before ANY storage call, even for an empty bucket
        clock.fresh()  # an unusable clock halts before storage as well
        resumed = pending(engine, budget)
        if not apply:
            if resumed:
                add('pending_fences', len(resumed))
        else:
            for proof in resumed:
                budget -= 1
                add(_finish(engine, storage, settings, clock, proof, run))
        start = read_cursor(engine)
        last, pages, exhausted = start, 0, False
        while budget > 0 and pages < files.orphan_max_pages:
            db = _session(engine)
            entry(db)  # every listing page follows a fresh entry proof
            entries, truncated = _page(storage, settings, last)
            pages += 1
            add('listed', len(entries))
            referenced, fenced = hints(engine, [e.key for e in entries])
            stopped = False
            for item in entries:
                if budget == 0:
                    stopped = True
                    break
                code, seen = _classify(item, referenced, fenced, clock.fresh(), files.orphan_grace)
                if code != 'candidate':
                    add(code)
                elif not apply:
                    budget -= 1
                    add('would_reconcile')
                else:
                    budget -= 1
                    add(_candidate(engine, storage, settings, clock, seen, run))
                last = item.key
            if not truncated and not stopped:
                exhausted = True
                break
        if budget == 0 and not exhausted:
            add('bound:deletes')
        elif pages >= files.orphan_max_pages and not exhausted:
            add('bound:pages')
        if apply:
            cursor = None if exhausted else last
            if cursor != start:
                write_cursor(engine, settings, clock, cursor)
    except runtime.Halt as halt:
        add('halted:' + halt.code)
    return counts
