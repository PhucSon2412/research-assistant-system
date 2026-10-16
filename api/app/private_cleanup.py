"""Private purge checkpoints. Storage I/O only after known commit and clean release."""
from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app import auth_safety, journal, publication, recovery
from app.api.private_delete import body_of
from app.audit import record_event
from app.ingest import runtime
from app.models import (PrivateItemDeletion, PrivateObjectFence, LibraryItem, PaperVersion, SourceFile,
                        ReviewRequest, UploadIntent, IngestionJob, CollectionItem)
from app.models.enums import AuditActorType, AuditOutcome
from app.clock import utcnow


LIVE = """SELECT count(*) FROM source_files f LEFT JOIN library_items i ON i.id=f.item_id
 WHERE f.storage_key=:k AND (i.id IS NULL OR
 (i.deleted_at IS NULL AND i.publication_state IS DISTINCT FROM 'revoked'))"""


def validate(db,body,*,deleted=False):
    """Snapshot tuples must match, including absent-target manifests. Never recreate a target."""
    item_id=uuid.UUID(body['item_id'])
    item=db.get(LibraryItem,item_id,populate_existing=True)
    if item is not None:
        if item.visibility!='private' or str(item.owner_user_id)!=body['owner_user_id']:
            raise recovery.RecoveryError('private_deletion_owner_conflict')
        if deleted and (item.deleted_at!=datetime.fromisoformat(body['deleted_at']) or item.rights_epoch!=body['rights_epoch']
            or item.active_version_id is not None or item.active_index_generation_id is not None or item.content_status!='metadata_only'):
            raise recovery.RecoveryError('private_deletion_not_applied')
    versions=set(body['manifest']['versions'])
    for version in versions:
        row=db.get(PaperVersion,uuid.UUID(version),populate_existing=True)
        if row is not None and row.item_id!=item_id:
            raise recovery.RecoveryError('private_deletion_version_conflict')
    files={f['id']:f for f in body['manifest']['files']}
    for ident,expected in files.items():
        row=db.get(SourceFile,uuid.UUID(ident),populate_existing=True)
        if row is not None and (row.item_id!=item_id or any(recovery._plain(getattr(row,k))!=v for k,v in expected.items())):
            raise recovery.RecoveryError('private_deletion_file_conflict')
    if any(str(v) not in versions for v in db.scalars(select(PaperVersion.id).where(PaperVersion.item_id==item_id))):
        raise recovery.RecoveryError('private_deletion_manifest_incomplete')
    if any(str(f) not in files for f in db.scalars(select(SourceFile.id).where(SourceFile.item_id==item_id))):
        raise recovery.RecoveryError('private_deletion_manifest_incomplete')
    return item


def replay(db,record):
    body=record.body; item_id=uuid.UUID(body['item_id'])
    # Recovery is quiescent. Keep the business lock order even here.
    db.execute(select(ReviewRequest.id).where(ReviewRequest.item_id==item_id).order_by(ReviewRequest.id).with_for_update()).all()
    db.execute(select(LibraryItem.id).where(LibraryItem.id==item_id).with_for_update()).all()
    for model in (IngestionJob,PaperVersion,SourceFile):
        db.execute(select(model.id).where(model.item_id==item_id).order_by(model.id).with_for_update()).all()
    item=validate(db,body)
    old=db.scalar(select(PrivateItemDeletion).where(PrivateItemDeletion.item_id==item_id))
    if old is not None and body_of(old)!=body:
        raise recovery.RecoveryError('private_deletion_conflict')
    at=datetime.fromisoformat(body['deleted_at'])
    if item is not None:
        if item.deleted_at is None:
            item.deleted_at=at; item.rights_epoch=body['rights_epoch']
            item.active_version_id=None; item.active_index_generation_id=None
            item.content_status='metadata_only'; item.updated_at=at
            db.flush()
        else:
            validate(db,body,deleted=True)
    if db.scalar(select(ReviewRequest.id).where(ReviewRequest.item_id==item_id,ReviewRequest.status=='open').limit(1)):
        # Every open grant must already have been closed by the preceding closure records.
        raise recovery.RecoveryError('private_deletion_review_still_open')
    jobs=list(db.scalars(select(IngestionJob).where(IngestionJob.item_id==item_id)))
    for job in jobs:
        job.cancel_requested=True; job.cancel_requested_at=job.cancel_requested_at or at
        job.lease_token=None; job.lease_until=None; job.next_attempt_at=None
    publication._finish_jobs(db,item_id,at)
    db.flush()  # restrictive job changes precede insertion of the new tombstone
    if old is None:
        db.add(PrivateItemDeletion(**{k:(uuid.UUID(v) if k in ('id','item_id','owner_user_id') else
            datetime.fromisoformat(v) if k in ('deleted_at','purge_due_at') else v) for k,v in body.items()}))
    db.flush()


def _checkpoint(engine,settings,operation,*,operator=False):
    db=Session(engine,expire_on_commit=False)
    if not operator:
        def entry(_):
            runtime.require_runtime(db)
            auth_safety.require_writer(db)
        runtime.short(db,entry)
    def execute(outcome):
        db.info['private_cleanup_phase']=operation.__name__
        db.execute(text(f'SET LOCAL lock_timeout = {int(settings.recovery.gate_lock_timeout_ms)}'))
        return operation(db,outcome)
    return runtime.short(db,execute)


def _final(db,settings,*,operator,writer=False):
    if operator:
        recovery._lock_journal(db)
    else:
        auth_safety.require_final(db,writer=writer)


def _confirmed(db,tomb):
    return bool(db.scalar(text("""SELECT EXISTS(SELECT 1 FROM revocation_journal_records
     WHERE kind='private_deletion' AND private_deletion_id=:id AND journal_confirmed
      AND ras_private_deletion_matches(line::jsonb->'body',:id))"""),{'id':tomb.id}))


def _proof(db,ident,expected=None):
    tomb=db.get(PrivateItemDeletion,ident,populate_existing=True)
    if tomb is None or not _confirmed(db,tomb) or (expected is not None and body_of(tomb)!=expected):
        raise runtime.Halt('private_cleanup_binding_changed')
    validate(db,body_of(tomb),deleted=True)
    return tomb


def _clock(tomb,clock):
    at=clock()
    if at<tomb.deleted_at:
        raise runtime.Halt('private_cleanup_clock_invalid')
    return at


def scrub(db,tomb,at):
    item_id=tomb.item_id
    publication._remove_derived(db,item_id,at)
    publication._finish_jobs(db,item_id,at)
    db.execute(delete(CollectionItem).where(CollectionItem.item_id==item_id))
    db.execute(update(LibraryItem).where(LibraryItem.id==item_id).values(title=None,active_version_id=None))
    db.execute(update(PaperVersion).where(PaperVersion.item_id==item_id).values(
        version_label=None,source_identifier=None,source_url=None,coverage=None))
    db.execute(update(SourceFile).where(SourceFile.item_id==item_id).values(original_filename=None,file_source=None))
    db.execute(update(ReviewRequest).where(ReviewRequest.item_id==item_id).values(declared_source=None,
        declared_rights=None,declared_source_url=None,declared_license_url=None,rejection_note=None))
    db.execute(update(UploadIntent).where(UploadIntent.item_id==item_id).values(filename=None))
    db.execute(update(IngestionJob).where(IngestionJob.item_id==item_id).values(source_label=None,warnings=[]))


def one(engine,storage,settings,ident,*,operator=False,sweep=False,clock=utcnow):
    def read(db,_):
        tomb=_proof(db,ident)
        if tomb.purged_at is not None and not sweep:
            return None
        _final(db,settings,operator=operator)
        _proof(db,ident); _clock(tomb,clock)
        return body_of(tomb)
    expected=_checkpoint(engine,settings,read,operator=operator)
    if expected is None: return None
    keys=sorted({f['storage_key'] for f in expected['manifest']['files']})
    results={}
    for key in keys:
        def prepare(db,outcome):
            tomb=_proof(db,ident,expected)
            result=db.scalar(text('SELECT ras_auth_private_cleanup_fence(:id,:key)'),{'id':ident,'key':key})
            _final(db,settings,operator=operator,writer=result=='fenced')
            _proof(db,ident,expected); _clock(tomb,clock)
            runtime.commit(db,outcome)
            return result
        result=_checkpoint(engine,settings,prepare,operator=operator)
        if result=='shared_kept':
            results[key]=result
            continue
        if result not in ('fenced','already_fenced'):
            raise runtime.Halt('private_cleanup_fence_unavailable')
        def pre_delete(db,_):
            _final(db,settings,operator=operator)
            tomb=_proof(db,ident,expected)
            if not db.scalar(select(PrivateObjectFence.id).where(PrivateObjectFence.storage_key==key)) or db.scalar(text(LIVE),{'k':key}):
                raise runtime.Halt('private_cleanup_reference_conflict')
            _clock(tomb,clock)
        _checkpoint(engine,settings,pre_delete,operator=operator)
        # Every Session has been released, including fence commit/rollback/close.
        results[key]=publication._remove_object(storage,key).value
    def finalize(db,outcome):
        tomb=db.scalar(select(PrivateItemDeletion).where(PrivateItemDeletion.id==ident)
                        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if tomb is None: return None
        db.execute(select(LibraryItem.id).where(LibraryItem.id==tomb.item_id).with_for_update()).all()
        _proof(db,ident,expected)
        for key,result in results.items():
            live=db.scalar(text(LIVE),{'k':key})
            if (result=='shared_kept') != bool(live):
                raise runtime.Halt('private_cleanup_reference_changed')
        at=clock()
        scrub(db,tomb,at)
        if tomb.purged_at is None:
            tomb.purged_at=at; tomb.cleanup_attempts+=1; tomb.last_cleanup_error=None
            record_event(db,action='private_item.cleanup',outcome=AuditOutcome.SUCCESS,actor_type=AuditActorType.SYSTEM,
                target_type='private_item_deletion',target_id=str(ident),details={'files':len(keys)})
        db.flush(); db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        _final(db,settings,operator=operator,writer=True)
        _proof(db,ident,expected); _clock(tomb,clock)
        runtime.commit(db,outcome)
        return 'purged'
    return _checkpoint(engine,settings,finalize,operator=operator)


def cleanup_private(engine,storage,settings,*,operator=False,sweep=False,clock=utcnow):
    if operator:
        publication._require_operator(engine)
    def candidates(db,_):
        stmt=select(PrivateItemDeletion.id)
        if not sweep: stmt=stmt.where(PrivateItemDeletion.purged_at.is_(None))
        ids=tuple(db.scalars(stmt.order_by(PrivateItemDeletion.purge_due_at,PrivateItemDeletion.id)
            .limit(settings.retention.cleanup_batch_size) if not sweep else stmt.order_by(PrivateItemDeletion.id)))
        _final(db,settings,operator=operator)
        return ids
    counts={}
    try:
        ids=_checkpoint(engine,settings,candidates,operator=operator)
        for ident in ids:
            try:
                result=one(engine,storage,settings,ident,operator=operator,sweep=sweep,clock=clock)
            except publication.CleanupError as exc:
                def error(db,outcome):
                    tomb=db.scalar(select(PrivateItemDeletion).where(PrivateItemDeletion.id==ident).with_for_update())
                    tomb.cleanup_attempts+=1; tomb.last_cleanup_error=exc.code
                    db.flush(); _final(db,settings,operator=operator,writer=True)
                    _proof(db,ident); _clock(tomb,clock); runtime.commit(db,outcome)
                _checkpoint(engine,settings,error,operator=operator)
                result='error:'+exc.code
            if result is not None: counts[result]=counts.get(result,0)+1
    except runtime.Halt as exc:
        counts['halted:'+exc.code]=1
    return counts
