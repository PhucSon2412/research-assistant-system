"""Owner deletion: one restrictive journal batch, known commit, fresh receipt."""
from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery
from app.api import common_publish, common_revoke
from app.api.errors import ApiError
from app.api.resource_writes import _actor
from app.api.upload_original import _thread
from app.db import get_engine
from app.identity.predicates import lock_users
from app.models import LibraryItem, PaperVersion, SourceFile, IngestionJob, ReviewRequest, PrivateItemDeletion
from app.models.enums import AuditActorType, AuditOutcome
from app.audit import record_event

_attempts: set[asyncio.Task] = set()


class AlreadyDeleted(Exception):
    def __init__(self,item_id):
        self.item_id=item_id


@dataclass
class Outcome:
    append_started: bool = False
    written: bool = False
    commit_started: bool = False
    committed: bool = False
    rollback_confirmed: bool = False
    teardown_failed: bool = False


def body_of(row):
    return {name: recovery._plain(getattr(row,name)) for name in journal.BODY_FIELDS[journal.PRIVATE_DELETION]}


def manifest(db,item_id):
    versions = list(db.scalars(select(PaperVersion.id).where(PaperVersion.item_id==item_id).order_by(PaperVersion.id)))
    files = db.execute(select(SourceFile.id,SourceFile.version_id,SourceFile.job_id,
        SourceFile.storage_key,SourceFile.checksum_sha256).where(SourceFile.item_id==item_id).order_by(SourceFile.id)).all()
    return {'versions':[str(v) for v in versions], 'files':[
        {name:recovery._plain(getattr(f,name)) for name in ('id','version_id','job_id','storage_key','checksum_sha256')}
        for f in files]}


def _current(db,principal,csrf,clock,settings):
    return _actor(db,principal,csrf,clock,settings.auth.session_idle)


def _gate(db,settings,closures=0):
    try:
        db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':recovery.JOURNAL_LOCK})
        state=journal.read(settings.recovery.journal_path)
        view=list(recovery._db_view(db))
        view[6]-=closures  # transaction-bound open -> withdrawn closures, before their batch mirror
        if not recovery.compare(state,tuple(view)).open or auth_safety.cutover(db)!='ready':
            raise ApiError(503,'service_recovering')
        return state
    except ApiError:
        raise
    except Exception:
        raise ApiError(503,'service_recovering') from None


def _binding(db,decision,closings):
    row=db.scalar(select(PrivateItemDeletion).where(PrivateItemDeletion.id==uuid.UUID(decision['id']))
                  .execution_options(populate_existing=True))
    item=db.scalar(select(LibraryItem).where(LibraryItem.id==uuid.UUID(decision['item_id']))
                   .execution_options(populate_existing=True))
    if (row is None or item is None or body_of(row)!=decision or manifest(db,item.id)!=decision['manifest']
        or item.visibility!='private' or str(item.owner_user_id)!=decision['owner_user_id']
        or item.deleted_at!=row.deleted_at or item.rights_epoch!=row.rights_epoch
        or item.active_version_id is not None or item.active_index_generation_id is not None
        or item.content_status!='metadata_only'
        or db.scalar(select(IngestionJob.id).where(IngestionJob.item_id==item.id,IngestionJob.cancel_requested.is_(False)).limit(1))
        or db.scalar(select(ReviewRequest.id).where(ReviewRequest.item_id==item.id,ReviewRequest.status=='open').limit(1))):
        raise ApiError(409,'deletion_binding_changed')
    for expected in closings:
        rr=db.get(ReviewRequest,uuid.UUID(expected['request_id']),populate_existing=True)
        if rr is None or recovery.closure_body(rr)!=expected or rr.closed_by_user_id!=item.owner_user_id:
            raise ApiError(409,'deletion_binding_changed')


def accept(principal,raw_id,csrf,clock,settings,stopped,outcome):
    db=Session(get_engine())
    db.info['private_deletion_phase']='accept'
    failure=None
    try:
        auth_safety.require_writer(db)
        common_publish._lock_timeout(db,settings)
        lock_users(db,[principal.user_id])
        current=_current(db,principal,csrf,clock,settings)
        def deny(status,code):
            _gate(db,settings);_current(db,principal,csrf,clock,settings)
            raise ApiError(status,code)
        try:
            item_id=uuid.UUID(str(raw_id))
        except ValueError:
            item_id=None
        hint=db.scalar(select(LibraryItem.id).where(LibraryItem.id==item_id,
            LibraryItem.visibility=='private',LibraryItem.owner_user_id==current.user_id))
        if hint is None:
            deny(404,'not_found')
        requests=list(db.scalars(select(ReviewRequest).where(ReviewRequest.item_id==item_id)
            .order_by(ReviewRequest.id).with_for_update()))
        item=db.scalar(select(LibraryItem).where(LibraryItem.id==item_id).with_for_update()
            .execution_options(populate_existing=True))
        previous=db.scalar(select(PrivateItemDeletion).where(PrivateItemDeletion.item_id==item_id))
        if item.deleted_at is not None:
            if previous is None or previous.owner_user_id!=current.user_id or previous.deleted_at!=item.deleted_at:
                raise ApiError(503,'deletion_binding_changed')
            _gate(db,settings); _current(db,principal,csrf,clock,settings)
            raise AlreadyDeleted(item_id)
        # Submit may have committed while we waited for the item. Never lock a newly
        # discovered request after item: refuse this attempt before any journal byte.
        ids=set(db.scalars(select(ReviewRequest.id).where(ReviewRequest.item_id==item_id)))
        if ids!={r.id for r in requests}:
            deny(409,'review_scope_changed')
        jobs=list(db.scalars(select(IngestionJob).where(IngestionJob.item_id==item_id)
                            .order_by(IngestionJob.id).with_for_update()))
        for model in (PaperVersion,SourceFile):
            db.execute(select(model.id).where(model.item_id==item_id).order_by(model.id).with_for_update()).all()
        content=manifest(db,item_id)
        closings=[r for r in requests if r.status=='open']
        limit=settings.retention.private_delete_manifest_limit
        if max(len(content['versions']),len(content['files']),len(closings))>limit:
            deny(409,'deletion_manifest_too_large')
        at=clock()
        item.deleted_at=at; item.rights_epoch+=1
        item.active_version_id=None; item.active_index_generation_id=None
        item.content_status='metadata_only'; item.updated_at=at
        for job in jobs:
            job.cancel_requested=True
            job.cancel_requested_at=job.cancel_requested_at or at
            job.lease_token=None; job.lease_until=None; job.next_attempt_at=None
            job.updated_at=at
        for rr in closings:
            rr.status='withdrawn'; rr.closed_at=at; rr.closed_by_user_id=current.user_id
            rr.close_reason_code='withdrawn_by_requester'; rr.rejection_note=None
        from app.publication import _finish_jobs
        _finish_jobs(db,item_id,at)
        tomb=PrivateItemDeletion(item_id=item_id,owner_user_id=current.user_id,rights_epoch=item.rights_epoch,
            deleted_at=at,purge_due_at=at+settings.retention.deleted_cleanup_deadline,manifest=content)
        db.add(tomb)
        record_event(db,action='private_item.delete',outcome=AuditOutcome.SUCCESS,actor_type=AuditActorType.USER,
            actor_user_id=current.user_id,target_type='library_item',target_id=str(item_id),
            details={'versions':len(content['versions']),'files':len(content['files']),'reviews_closed':len(closings)})
        db.flush()
        decision=body_of(tomb)
        closed=[recovery.closure_body(r) for r in closings]
        state=_gate(db,settings,len(closings))
        with db.no_autoflush:
            _binding(db,decision,closed)
            _current(db,principal,csrf,clock,settings)
            records=[]; head=state.head
            for kind,body in [(journal.REVIEW_CLOSURE,b) for b in closed]+[(journal.PRIVATE_DELETION,decision)]:
                head=journal.next_record(head,kind,body); records.append(head)
            lines=[r.line for r in records]
            db.execute(text('SELECT ras_auth_private_delete_mirror(:lines)'),{'lines':lines})
            _binding(db,decision,closed)
            _current(db,principal,csrf,clock,settings)
            if stopped.is_set():
                raise common_publish.Stopped()
            outcome.append_started=True
            try:
                journal.append(settings.recovery.journal_path,state,records)
            except (OSError,journal.JournalError):
                pass  # restriction + false mirrors commit; normal gate stays closed until recovery
            else:
                outcome.written=True
                db.execute(text('SELECT ras_auth_private_delete_confirm(:lines)'),{'lines':lines})
                _gate(db,settings)
            # Every append result, including fsync failure, gets a fresh final
            # binding/caller/clock proof. A failed proof rolls back DB state; any
            # journal prefix can only replay restrictions during recovery.
            _binding(db,decision,closed)
            _current(db,principal,csrf,clock,settings)
        outcome.commit_started=True
        db.commit()
        outcome.committed=True
    except BaseException as exc:
        failure=exc
    finally:
        common_revoke._release(db,outcome)
    if isinstance(failure,AlreadyDeleted) and not outcome.teardown_failed:
        return failure.item_id
    if outcome.commit_started and not outcome.committed:
        raise ApiError(503,'deletion_outcome_uncertain') from None
    if outcome.committed and not outcome.written:
        raise ApiError(503,'deletion_journal_unavailable') from None
    if isinstance(failure,ApiError):
        if outcome.append_started:
            raise ApiError(503,'deletion_pending_recovery') from None
        raise failure
    if failure is not None and not isinstance(failure,Exception):
        raise failure
    if failure is not None or outcome.teardown_failed:
        raise ApiError(503,'deletion_response_unavailable' if outcome.committed else
            'deletion_pending_recovery' if outcome.append_started else 'deletion_unavailable') from None
    return item_id


def respond(principal,item_id,csrf,clock,settings,outcome):
    db=Session(get_engine()); response=Outcome(); result=failure=None
    db.info['private_deletion_phase']='response'
    try:
        common_publish._lock_timeout(db,settings)
        # Receipt uses a separate transaction and never grants content access.
        item=db.scalar(select(LibraryItem).where(LibraryItem.id==item_id).with_for_update(read=True))
        tomb=db.scalar(select(PrivateItemDeletion).where(PrivateItemDeletion.item_id==item_id))
        auth_safety.require_final(db)
        current=_current(db,principal,csrf,clock,settings)
        if (item is None or tomb is None or tomb.owner_user_id!=current.user_id or item.owner_user_id!=current.user_id
            or item.deleted_at!=tomb.deleted_at or item.rights_epoch!=tomb.rights_epoch):
            raise ApiError(409,'deletion_binding_changed')
        result={'item_id':str(item_id),'deleted':True,'purge_due_at':tomb.purge_due_at.isoformat(),
                'cleanup_status':'completed' if tomb.purged_at else 'pending'}
        _current(db,principal,csrf,clock,settings)
    except BaseException as exc:
        failure=exc
    finally:
        common_revoke._release(db,response)
    if failure is not None or response.teardown_failed:
        raise ApiError(503,'deletion_response_unavailable') from None
    return result


async def attempt(request,principal,raw_id,csrf,clock,settings,stopped,outcome=None):
    outcome=outcome or Outcome()
    try:
        item_id=await _thread(stopped,accept,principal,raw_id,csrf,clock,settings,stopped,outcome)
        if stopped.is_set() or await common_publish._disconnected(request):
            raise ApiError(503,'deletion_response_unavailable')
        return await _thread(stopped,respond,principal,item_id,csrf,clock,settings,outcome)
    except ApiError:
        raise
    except Exception:
        raise ApiError(503,'deletion_outcome_uncertain' if outcome.commit_started and not outcome.committed
            else 'deletion_response_unavailable' if outcome.committed else 'deletion_unavailable') from None


async def receive(request,principal,raw_id,clock,settings):
    stopped=threading.Event()
    task=asyncio.create_task(attempt(request,principal,raw_id,request.headers.get('x-csrf-token') or '',clock,settings,stopped))
    _attempts.add(task)
    def settled(done):
        _attempts.discard(done)
        if not done.cancelled(): done.exception()
    task.add_done_callback(settled)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        raise
