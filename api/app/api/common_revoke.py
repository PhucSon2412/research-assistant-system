"""The single runtime revoke POST. No storage, provider, replay or commit inference.

The attempt owns outcome and its executor until settled. Once append starts,
disconnect does not undo the restriction: finish this attempt's transaction;
durable intent with a precommit denial is left for recovery only.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety, journal, publication, recovery
from app.api import common_publish
from app.api.errors import ApiError
from app.api.upload_original import _thread
from app.db import get_engine
from app.identity.predicates import lock_users
from app.models import (AccessRights, CommonPublication, DeletionTombstone, IngestionJob,
                        LibraryItem, PaperVersion, RevocationMetadataRetention, SourceFile)
from app.models.enums import AuditOutcome

_attempts: set[asyncio.Task] = set()


@dataclass
class Outcome:
    body: dict | None = None
    record: journal.Record | None = None
    append_started: bool = False
    written: bool = False
    commit_started: bool = False
    committed: bool = False
    rollback_confirmed: bool = False
    teardown_failed: bool = False
    cancelled_jobs: int = 0


def _current(db, principal, csrf, clock, settings):
    return common_publish._current(db, principal, csrf, clock, settings)


def _gate(db, settings):
    # The staged tombstone is not an unjournaled auth/review closure. Subtract
    # nothing: all other unconfirmed/hold/chain/cutover failures still close.
    try:
        db.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': recovery.JOURNAL_LOCK})
        state = journal.read(settings.recovery.journal_path)
        if not recovery.compare(state, recovery._db_view(db)).open or auth_safety.cutover(db) != 'ready':
            raise ApiError(503, 'service_recovering')
        return state
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, 'service_recovering') from None


def _release(db, outcome):
    for method in ('rollback', 'close'):
        try:
            getattr(db, method)()
            if method == 'rollback' and not outcome.commit_started:
                outcome.rollback_confirmed = True
        except BaseException:
            outcome.teardown_failed = True
    if outcome.teardown_failed:
        try:
            db.invalidate()
        except BaseException:
            pass


def _jobs(db, item_id):
    return tuple(db.execute(select(IngestionJob.id, IngestionJob.cancel_requested,
        IngestionJob.cancel_requested_at, IngestionJob.finished_at, IngestionJob.updated_at)
        .where(IngestionJob.item_id == item_id).order_by(IngestionJob.id)).all())


def _binding(db, body, jobs):
    """Independent Python binding; no new locks, autoflush or line-derived IDs."""
    tomb = db.scalar(select(DeletionTombstone).where(DeletionTombstone.id == uuid.UUID(body['id']))
                     .execution_options(populate_existing=True))
    bases = list(db.scalars(select(RevocationMetadataRetention).where(
        RevocationMetadataRetention.tombstone_id == uuid.UUID(body['id']))
        .execution_options(populate_existing=True)))
    if tomb is None or recovery.revocation_body(tomb, bases) != body:
        raise ApiError(409, 'revocation_binding_changed')
    row = db.execute(text("""SELECT i.revocation_transaction_id=txid_current()
      AND i.revocation_prior_state=:prior AND i.revocation_prior_epoch+1=:epoch
      AND i.visibility='common' AND i.owner_user_id IS NULL AND i.publication_state='revoked'
      AND i.content_status='metadata_only' AND i.active_index_generation_id IS NULL
      AND i.rights_epoch=:epoch AND i.updated_at=:at
      AND a.id=:ar AND a.version_id=:v AND a.source_file_id=:f
      AND a.revoked_at=:at AND a.revoked_by_user_id=:actor AND a.revocation_reason_code=:reason
      AND a.rights_epoch_at_revoke=:epoch AND a.rights_epoch_at_grant=i.revocation_prior_epoch
      AND f.storage_key=:key AND f.checksum_sha256=a.file_checksum_sha256
      AND t.xmin=(txid_current()%4294967296)::text::xid
      AND i.xmin=t.xmin AND a.xmin=t.xmin
      FROM library_items i JOIN access_rights a ON a.item_id=i.id
      JOIN paper_versions v ON v.id=a.version_id AND v.item_id=i.id
      JOIN source_files f ON f.id=a.source_file_id AND f.item_id=i.id AND f.version_id=v.id
      JOIN deletion_tombstones t ON t.item_id=i.id AND t.id=:t WHERE i.id=:i"""),
      dict(prior=body['prior_publication_state'], epoch=body['rights_epoch'], at=tomb.recorded_at,
           ar=tomb.access_rights_id, v=tomb.version_id, f=tomb.source_file_id,
           actor=tomb.recorded_by_user_id, reason=tomb.reason_code, key=tomb.storage_key,
           t=tomb.id, i=tomb.item_id)).all()
    if len(row) != 1 or row[0][0] is not True or _jobs(db, tomb.item_id) != jobs:
        raise ApiError(409, 'revocation_binding_changed')
    if any(not j.cancel_requested for j in jobs if j.finished_at is None):
        raise ApiError(409, 'revocation_binding_changed')
    pub = db.execute(select(CommonPublication.access_rights_id, CommonPublication.published_at,
        CommonPublication.revoked_at).where(CommonPublication.item_id == tomb.item_id)).one_or_none()
    if (body['prior_publication_state'] == 'listed' and pub is None or
            body['prior_publication_state'] == 'pending' and pub is not None or
            pub is not None and (pub.access_rights_id != tomb.access_rights_id or
                                pub.revoked_at != max(tomb.recorded_at, pub.published_at))):
        raise ApiError(409, 'revocation_binding_changed')
    for model in (AccessRights, PaperVersion, SourceFile):
        if len(db.execute(select(model.id).where(model.item_id == tomb.item_id)).all()) != 1:
            raise ApiError(409, 'revocation_binding_changed')
    for basis in bases:
        if basis.item_id != tomb.item_id or basis.confirmed_by_user_id != tomb.recorded_by_user_id or basis.recorded_at != tomb.recorded_at:
            raise ApiError(409, 'revocation_binding_changed')


def accept(principal, raw_id, body, csrf, clock, settings, stopped, outcome):
    db = Session(get_engine())
    failure = None
    try:
        auth_safety.require_writer(db)
        common_publish._lock_timeout(db, settings)
        lock_users(db, [principal.user_id])
        current = _current(db, principal, csrf, clock, settings)

        def deny(status, code):
            _gate(db, settings)
            _current(db, principal, csrf, clock, settings)
            raise ApiError(status, code)

        try:
            item_id = uuid.UUID(str(raw_id))
        except ValueError:
            deny(404, 'not_found')
        item = publication._copy(db, current, item_id, lock=True)
        if item is None:
            deny(404, 'not_found')
        # Never lock a pre-existing tombstone after item (cleanup uses the reverse).
        if item.publication_state == 'revoked' or db.scalar(select(DeletionTombstone.id).where(DeletionTombstone.item_id == item_id)):
            deny(409, 'common_copy_revoked')
        rights = list(db.scalars(select(AccessRights).where(AccessRights.item_id == item_id)
            .order_by(AccessRights.id).with_for_update()))
        pub = db.scalar(select(CommonPublication).where(CommonPublication.item_id == item_id).with_for_update())
        db.execute(select(IngestionJob.id).where(IngestionJob.item_id == item_id)
                   .order_by(IngestionJob.id).with_for_update()).all()
        for model in (PaperVersion, SourceFile):
            db.execute(select(model.id).where(model.item_id == item_id).order_by(model.id).with_for_update()).all()
        if len(rights) != 1:
            deny(409, 'revocation_target_invalid')
        first = rights[0]
        source = db.get(SourceFile, first.source_file_id)
        version = db.get(PaperVersion, first.version_id)
        if (source is None or version is None or source.item_id != item_id or version.item_id != item_id
                or source.version_id != version.id or source.checksum_sha256 != first.file_checksum_sha256
                or first.revoked_at is not None or first.rights_epoch_at_grant != item.rights_epoch
                or item.publication_state not in ('pending', 'listed')
                or (item.publication_state == 'listed') != (pub is not None)):
            deny(409, 'revocation_target_invalid')
        fields = [r.field for r in body.retain]
        if len(fields) != len(set(fields)):
            deny(422, 'retention_field_repeated')
        if body.retain and body.confirm_retention_basis is not True:
            deny(422, 'retention_confirmation_required')
        if stopped.is_set():
            raise common_publish.Stopped()
        prior, at = item.publication_state, clock()
        publication._revoke_rows(db, item, rights, current.user_id, body.reason_code.value, at, item.rights_epoch + 1)
        outcome.cancelled_jobs = publication._cancel_jobs(db, item_id, at)
        tomb = DeletionTombstone(target_kind='common_copy', item_id=item_id, version_id=first.version_id,
            source_file_id=first.source_file_id, storage_key=source.storage_key, access_rights_id=first.id,
            reason_code=body.reason_code.value, prior_publication_state=prior, rights_epoch=item.rights_epoch,
            recorded_at=at, recorded_by_user_id=current.user_id, purge_due_at=at+settings.retention.deleted_cleanup_deadline)
        db.add(tomb); db.flush()
        bases = [RevocationMetadataRetention(tombstone_id=tomb.id, item_id=item_id, field=r.field.value,
            policy_source=r.policy_source, scope=r.scope, reason=r.reason,
            confirmed_by_user_id=current.user_id, recorded_at=at) for r in body.retain]
        db.add_all(bases)
        publication._audit(db, current, 'common_copy.revoke', AuditOutcome.SUCCESS, item_id,
            reason_code=body.reason_code.value, from_state=prior, tombstone_id=str(tomb.id),
            cancelled_jobs=outcome.cancelled_jobs, retained_fields=sorted(r.field.value for r in body.retain))
        db.flush()  # all DML/FK/audit before journal; deferred mirror fence follows
        outcome.body = recovery.revocation_body(tomb, bases)
        jobs = _jobs(db, item_id)
        state = _gate(db, settings)  # exclusive directly
        with db.no_autoflush:
            _binding(db, outcome.body, jobs)
            _current(db, principal, csrf, clock, settings)
            outcome.record = journal.next_record(state.head, journal.REVOCATION, outcome.body)
            db.execute(text('SELECT ras_auth_common_revocation_mirror(:line)'), {'line': outcome.record.line})
            # Routine SQL/constraints may wait. The authorization boundary is
            # AFTER that final SQL and immediately BEFORE the first byte.
            _binding(db, outcome.body, jobs)
            _current(db, principal, csrf, clock, settings)
            if stopped.is_set():
                raise common_publish.Stopped()
            outcome.append_started = True
            try:
                journal.append(settings.recovery.journal_path, state, [outcome.record])
            except (OSError, journal.JournalError):
                # Scoped restriction-only boundary: do not normal-gate/actor
                # recheck here and accidentally roll back the false mirror.
                pass
            else:
                outcome.written = True
                db.execute(text('SELECT ras_auth_common_revocation_confirm(:line)'), {'line': outcome.record.line})
                _gate(db, settings)
                _binding(db, outcome.body, jobs)
                _current(db, principal, csrf, clock, settings)
        outcome.commit_started = True
        db.commit()  # t_rev only when this returns (remote exception is uncertain)
        outcome.committed = True
    except BaseException as exc:
        failure = exc
    finally:
        _release(db, outcome)
    if outcome.commit_started and not outcome.committed:
        raise ApiError(503, 'revocation_outcome_uncertain') from None
    if outcome.committed and not outcome.written:
        raise ApiError(503, 'revocation_journal_unavailable') from None
    if isinstance(failure, ApiError):
        raise failure
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    if failure is not None or outcome.teardown_failed:
        raise ApiError(503, 'revocation_response_unavailable' if outcome.committed else
                       'revocation_pending_recovery' if outcome.append_started else 'revocation_unavailable') from None
    return item_id


def respond(principal, item_id, csrf, clock, settings, outcome):
    from app.api.routes import publications
    db = Session(get_engine())  # never reuse acceptance identities or transaction
    failure = result = None
    response_outcome = Outcome()
    try:
        common_publish._lock_timeout(db, settings)
        locked = common_publish._lock_item(db, principal, item_id)
        if locked is not None:
            common_publish._lock_resources(db, item_id)
        auth_safety.require_final(db)
        current = _current(db, principal, csrf, clock, settings)
        if locked is None:
            raise ApiError(409, 'resource_scope_changed')
        try:
            dto = publications._copy_out(db, current, item_id, clock())
        except ApiError:
            # A formatter/resource denial may follow waiting SQL too.
            _current(db, principal, csrf, clock, settings)
            raise
        publications._read_cleanup_clock([dto], clock)
        _current(db, principal, csrf, clock, settings)
        result = {'item': dto, 'cancelled_jobs': outcome.cancelled_jobs}
    except BaseException as exc:
        failure = exc
    finally:
        _release(db, response_outcome)
        outcome.teardown_failed |= response_outcome.teardown_failed
    if isinstance(failure, ApiError):
        raise failure
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    if failure is not None or response_outcome.teardown_failed:
        raise ApiError(503, 'revocation_response_unavailable') from None
    return result


async def attempt(request, principal, raw_id, body, csrf, clock, settings, stopped, outcome=None):
    outcome = outcome if outcome is not None else Outcome()
    try:
        item_id = await _thread(stopped, accept, principal, raw_id, body, csrf, clock, settings, stopped, outcome)
        if stopped.is_set() or await common_publish._disconnected(request):
            raise ApiError(503, 'revocation_response_unavailable')
        return await _thread(stopped, respond, principal, item_id, csrf, clock, settings, outcome)
    except ApiError:
        raise
    except Exception:
        raise ApiError(503, 'revocation_outcome_uncertain' if outcome.commit_started and not outcome.committed else
                       'revocation_response_unavailable' if outcome.committed else
                       'revocation_pending_recovery' if outcome.append_started else 'revocation_unavailable') from None


async def receive(request, principal, raw_id, body, clock, settings):
    stopped = threading.Event()
    task = asyncio.create_task(attempt(request, principal, raw_id, body,
        request.headers.get('x-csrf-token') or '', clock, settings, stopped))
    _attempts.add(task)

    def settled(done):
        _attempts.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(settled)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        raise
