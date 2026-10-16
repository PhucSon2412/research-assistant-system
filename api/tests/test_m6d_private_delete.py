"""M6d business uses real runtime roles/journal/readiness; storage faults are labelled fakes."""
import asyncio
import copy
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import journal, private_cleanup, recovery
from app.api import private_delete as boundary
from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_engine
from app.models import LibraryItem, PaperVersion, SourceFile, PrivateItemDeletion
from tests.resource_fixtures import source, derived, BODY
from tests.test_c2c1_bootstrap import installation  # noqa
from tests.test_c2c2_invitations import operational_world as world  # noqa
from tests.test_c2d1_final_caller import browser  # noqa
from tests.test_c2d5a_review_writes import writers, opened, request as review_request  # noqa
from tests.test_c2d5b_review_approval import Originals, approve, body as approval_body


class Storage(Originals):
    def size(self,key):
        if key not in self.objects:
            raise ClientError({'Error':{'Code':'NoSuchKey'}},'HeadObject')
        return len(self.objects[key])


@pytest.fixture
def deletion(writers,monkeypatch):
    b=writers
    b.own=source(b.w,b.user.id,'M6D-PRIVATE-TITLE',storage_key='originals/'+uuid.uuid4().hex)
    b.body=b.body.model_copy(update=dict(item_id=b.own.item,version_id=b.own.version,source_file_id=b.own.file))
    b.storage=Storage(b.own)
    from app.main import app
    from app.api.routes import reviews
    app.dependency_overrides[reviews._storage]=lambda:b.storage
    return b


def scalar(b,sql,**params):
    with b.w.engine.connect() as c:return c.scalar(text(sql),params)


def remove(b,*,who='owner',data=None,item=None,headers=None):
    issued=b.owner_session if who=='owner' else b.w.actor
    b.client.cookies.set('ras_session',issued.token); b.client.cookies.set('ras_csrf',issued.csrf_token)
    return b.client.post('/library/items/'+str(item or b.own.item)+'/delete',
        json={'confirm_delete':True} if data is None else data,
        headers={'Origin':'http://web.test','X-CSRF-Token':issued.csrf_token}|(headers or {}))


def direct(b,outcome=None):
    return asyncio.run(boundary.attempt(None,b.owner,str(b.own.item),b.owner_session.csrf_token,
        b.clock,get_settings(),threading.Event(),outcome))


def owner_get(b,path):
    b.client.cookies.set('ras_session',b.owner_session.token)
    return b.client.get(path)


def purge(b,**kwargs):
    return private_cleanup.cleanup_private(b.w.runtime,b.storage,get_settings(),clock=b.clock,**kwargs)


def test_owner_atomic_closure_denial_idempotent_and_real_ready(deletion):
    b=deletion; opened(b); derived(b.w,b.own,'M6D-PRIVATE-BODY')
    old_head=journal.read(b.w.path).head.seq
    assert owner_get(b,f'/library/items/{b.own.item}').status_code==200
    r=remove(b)
    assert r.status_code==200,r.text
    assert r.json()['deleted'] is True and r.json()['cleanup_status']=='pending'
    assert b.storage.deleted==[]
    assert scalar(b,'SELECT rights_epoch FROM library_items WHERE id=:i',i=b.own.item)==1
    assert scalar(b,'SELECT status FROM review_requests WHERE id=:r',r=b.rid)=='withdrawn'
    assert [r.kind for r in journal.read(b.w.path).records if r.seq>old_head]==['review_closure','private_deletion']
    assert recovery.evaluate(get_settings(),b.w.runtime).open
    assert b.client.get('/readyz').status_code==200
    for suffix in ('','/files','/chunks','/coverage','/references','/metadata-candidates'):
        response=owner_get(b,f'/library/items/{b.own.item}'+suffix)
        assert response.status_code==404,(suffix,response.text)
    assert owner_get(b,f'/files/{b.own.file}/original').status_code==404
    assert str(b.own.item) not in owner_get(b,'/library/items').text
    assert owner_get(b,f'/library/items/{b.other.item}').status_code==404
    b.client.cookies.set('ras_session',b.w.actor.token)
    assert b.client.get(f'/library/items/{b.other.item}').status_code==200  # active Admin-owned control
    assert b.client.get(f'/admin/review-requests/{b.rid}').json()['request']['review_available'] is False
    before=b.w.path.read_bytes()
    assert remove(b).json()==r.json()
    assert b.w.path.read_bytes()==before


def test_openapi_confirmation_and_receipt_are_explicit():
    from app.main import app
    spec=app.openapi();operation=spec['paths']['/library/items/{item_id}/delete']['post']
    schema=spec['components']['schemas']['DeleteInput']
    assert schema['additionalProperties'] is False
    assert schema['properties']['confirm_delete']['const'] is True
    assert schema['required']==['confirm_delete']
    assert operation['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/DeleteReceipt')


@pytest.mark.parametrize('value',[False,1,1.0,'true',None,[],{}])
def test_strict_confirmation_no_mutation(deletion,value):
    b=deletion; before=b.w.path.read_bytes()
    assert remove(b,data={'confirm_delete':value}).status_code==422
    assert b.w.path.read_bytes()==before
    assert scalar(b,'SELECT deleted_at FROM library_items WHERE id=:i',i=b.own.item) is None


@pytest.mark.parametrize('data',[{}, {'confirm_delete':True,'force':True}])
def test_missing_extra_confirmation(deletion,data):
    assert remove(deletion,data=data).status_code==422


@pytest.mark.parametrize('kind',['admin','missing','common'])
def test_non_owner_or_common_hidden(deletion,kind):
    b=deletion; ident=b.own.item
    if kind=='missing':ident=uuid.uuid4()
    if kind=='common':
        with b.w.engine.begin() as c:
            ident=c.scalar(text("INSERT INTO library_items(visibility,publication_state) VALUES('common','listed') RETURNING id"))
    before=b.w.path.read_bytes()
    assert remove(b,who='admin' if kind=='admin' else 'owner',item=ident).status_code==404
    assert b.w.path.read_bytes()==before


@pytest.mark.parametrize('headers',[{'Origin':'https://untrusted.test'},{'X-CSRF-Token':'wrong'}])
def test_origin_csrf_denied(deletion,headers):
    b=deletion; before=b.w.path.read_bytes()
    assert remove(b,headers=headers).status_code==403
    assert b.w.path.read_bytes()==before


def test_multiversion_purge_scrubs_all_and_preserves_shared_key(deletion):
    b=deletion; opened(b); derived(b.w,b.own,'M6D-PRIVATE-BODY')
    with Session(b.w.engine) as db:
        v=PaperVersion(item_id=b.own.item,source_url='https://private.example.test',version_label='SECRET-V2')
        db.add(v);db.flush()
        f=SourceFile(item_id=b.own.item,version_id=v.id,storage_key='originals/'+uuid.uuid4().hex,
            checksum_sha256=b.body.model_dump().get('checksum','a'*64),byte_size=5,verified_mime='text/markdown',
            original_filename='SECRET.md',file_source='SECRET-SOURCE')
        db.add(f);db.commit(); second_key=f.storage_key
    shared=source(b.w,b.w.uid,'B-CONTROL',storage_key=b.own.key)
    b.storage.objects[second_key]=b'hello'
    assert remove(b).status_code==200
    assert purge(b)=={'purged':1}
    assert b.own.key not in b.storage.deleted and second_key in b.storage.deleted
    assert scalar(b,'SELECT title FROM library_items WHERE id=:i',i=shared.item)=='B-CONTROL'
    assert scalar(b,'SELECT title FROM library_items WHERE id=:i',i=b.own.item) is None
    assert scalar(b,'SELECT declared_source FROM review_requests WHERE id=:r',r=b.rid) is None
    assert scalar(b,'SELECT count(*) FROM chunks WHERE item_id=:i',i=b.own.item)==0
    assert scalar(b,"SELECT count(*) FROM source_files WHERE item_id=:i AND num_nonnulls(original_filename,file_source)>0",i=b.own.item)==0
    assert purge(b)=={}
    assert remove(b).json()['cleanup_status']=='completed'


def test_deleted_id_version_late_derived_and_key_cannot_reappear(deletion):
    b=deletion; assert remove(b).status_code==200; assert purge(b)=={'purged':1}
    for sql in ["UPDATE library_items SET deleted_at=NULL WHERE id=:i",
                "UPDATE library_items SET title='resurrected' WHERE id=:i",
                "INSERT INTO paper_versions(item_id) VALUES(:i)",
                "UPDATE paper_versions SET source_url='https://resurrected.test' WHERE item_id=:i",
                "UPDATE paper_versions SET coverage='complete' WHERE item_id=:i",
                "UPDATE source_files SET original_filename='resurrected' WHERE item_id=:i",
                "DELETE FROM private_item_deletions WHERE item_id=:i"]:
        with pytest.raises(DBAPIError),b.w.engine.begin() as c:c.execute(text(sql),{'i':b.own.item})
    with pytest.raises(DBAPIError):derived(b.w,b.own,'RESURRECTED-BODY')
    with pytest.raises(DBAPIError):source(b.w,b.user.id,'OTHER',storage_key=b.own.key)
    fresh=source(b.w,b.user.id,'NEW-CONSENT-CONTROL',storage_key='originals/'+uuid.uuid4().hex)
    assert fresh.item!=b.own.item


@pytest.mark.parametrize('mode',['none','torn','full'])
def test_append_failure_commits_false_mirrors_recovers_restriction(deletion,monkeypatch,mode):
    b=deletion; opened(b); original=journal.append
    def broken(path,state,records):
        if mode=='full':original(path,state,records)
        elif mode=='torn':
            with path.open('ab') as f:f.write(records[0].line[:20].encode())
        raise OSError('synthetic fsync fault')
    monkeypatch.setattr(journal,'append',broken)
    response=remove(b)
    assert response.status_code==503 and response.json()['error']=='deletion_journal_unavailable',response.text
    assert scalar(b,'SELECT deleted_at FROM library_items WHERE id=:i',i=b.own.item) is not None
    assert scalar(b,"SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed")==2
    assert not recovery.evaluate(get_settings(),b.w.runtime).open
    monkeypatch.setattr(journal,'append',original)
    report=recovery.recover(b.w.engine,b.storage,get_settings(),b.clock(),confirm_db_current=mode!='full')
    assert report.safety_ready
    assert owner_get(b,f'/library/items/{b.own.item}').status_code==404
    assert b.own.key in b.storage.deleted


@pytest.mark.parametrize('landed',[False,True])
def test_commit_uncertain_no_select_inference_or_success(deletion,monkeypatch,landed):
    b=deletion; outcome=boundary.Outcome(); real=Session.commit
    def commit(db):
        if db.bind is get_engine() and db.scalar(text("SELECT count(*) FROM private_item_deletions")):
            if landed:real(db)
            raise OSError('synthetic commit uncertainty')
        return real(db)
    monkeypatch.setattr(Session,'commit',commit)
    with pytest.raises(ApiError) as exc:direct(b,outcome)
    assert exc.value.code=='deletion_outcome_uncertain'
    assert outcome.commit_started and not outcome.committed
    monkeypatch.setattr(Session,'commit',real)
    assert recovery.recover(b.w.engine,b.storage,get_settings(),b.clock()).safety_ready
    assert owner_get(b,f'/library/items/{b.own.item}').status_code==404


def test_restore_old_target_with_current_journal_twice(deletion):
    b=deletion; opened(b); derived(b.w,b.own,'M6D-RESTORE-BODY')
    # Privileged snapshot-corruption setup only. Actual full pg_dump/object drill is separate.
    assert remove(b).status_code==200
    record=journal.read(b.w.path).head
    with b.w.engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role='replica'"))
        c.execute(text('DELETE FROM revocation_journal_records WHERE seq=:s'),{'s':record.seq})
        c.execute(text('DELETE FROM private_item_deletions WHERE item_id=:i'),{'i':b.own.item})
        c.execute(text('UPDATE library_items SET deleted_at=NULL,deletion_transaction_id=NULL,rights_epoch=0 WHERE id=:i'),{'i':b.own.item})
    assert not recovery.evaluate(get_settings(),b.w.runtime).open
    for _ in range(2):
        b.storage.objects[b.own.key]=BODY
        assert recovery.recover(b.w.engine,b.storage,get_settings(),b.clock()).safety_ready
        assert b.own.key not in b.storage.objects
        assert owner_get(b,f'/library/items/{b.own.item}').status_code==404


def test_approve_first_common_copy_survives_private_delete(deletion):
    b=deletion; opened(b)
    response=approve(b,approval_body())
    assert response.status_code==200,response.text
    common=response.json()['request']['pending_publication_id']
    assert remove(b).status_code==200
    assert purge(b)=={'purged':1}
    assert scalar(b,'SELECT publication_state FROM library_items WHERE id=:i',i=common)=='pending'
    assert scalar(b,'SELECT revoked_at FROM access_rights WHERE item_id=:i',i=common) is None


def test_delete_first_approval_never_copies(deletion):
    b=deletion; opened(b); assert remove(b).status_code==200
    assert approve(b,approval_body()).status_code in (404,409)
    assert not b.storage.puts


def test_two_deletions_one_decision(deletion):
    b=deletion
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:direct(b),range(2)))
    assert all(r['deleted'] for r in results)
    assert scalar(b,"SELECT count(*) FROM revocation_journal_records WHERE kind='private_deletion'")==1
    assert scalar(b,'SELECT rights_epoch FROM library_items WHERE id=:i',i=b.own.item)==1


@pytest.mark.parametrize('mode',['none','prefix','full'])
@pytest.mark.parametrize('landed',[False,True])
def test_partial_journal_and_commit_uncertain_replay_only_complete_restrictions(deletion,monkeypatch,mode,landed):
    b=deletion;opened(b);original_append=journal.append;original_commit=Session.commit
    def append(path,state,records):
        if mode!='none':original_append(path,state,records[:1] if mode=='prefix' else records)
        raise OSError('injected append fault')
    def commit(db):
        if db.info.get('private_deletion_phase')=='accept':
            if landed:original_commit(db)
            raise OSError('injected commit uncertainty')
        return original_commit(db)
    monkeypatch.setattr(journal,'append',append);monkeypatch.setattr(Session,'commit',commit)
    assert remove(b).json()['error']=='deletion_outcome_uncertain'
    monkeypatch.setattr(journal,'append',original_append);monkeypatch.setattr(Session,'commit',original_commit)
    report=recovery.recover(b.w.engine,b.storage,get_settings(),b.clock(),confirm_db_current=landed and mode!='full')
    assert report.safety_ready
    deleted=landed or mode=='full'
    assert owner_get(b,f'/library/items/{b.own.item}').status_code==(404 if deleted else 200)
    status=scalar(b,'SELECT status FROM review_requests WHERE id=:id',id=b.rid)
    assert (status!='open')==(landed or mode!='none')


def test_actual_fsync_failure_full_line_not_confirmed_by_matching_bytes(deletion,monkeypatch):
    b=deletion; real=journal.os.fsync
    monkeypatch.setattr(journal.os,'fsync',lambda fd:(_ for _ in ()).throw(OSError('injected os.fsync')))
    r=remove(b)
    assert r.status_code==503 and r.json()['error']=='deletion_journal_unavailable'
    assert journal.read(b.w.path).head.kind==journal.PRIVATE_DELETION
    assert scalar(b,'SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed')==1
    monkeypatch.setattr(journal.os,'fsync',real)
    assert recovery.recover(b.w.engine,b.storage,get_settings(),b.clock()).safety_ready


@pytest.mark.parametrize('phase',['accept','response'])
@pytest.mark.parametrize('interrupt',[False,True])
def test_known_commit_release_failure_never_reports_rollback_or_success(deletion,monkeypatch,phase,interrupt):
    b=deletion;real=Session.close;fired=[]
    def close(db):
        real(db)
        if db.info.get('private_deletion_phase')==phase and not fired:
            fired.append(True)
            if interrupt:raise KeyboardInterrupt()
            raise OSError('injected release fault')
    monkeypatch.setattr(Session,'close',close)
    r=remove(b)
    assert fired and r.status_code==503 and r.json()['error']=='deletion_response_unavailable',r.text
    assert scalar(b,'SELECT deleted_at FROM library_items WHERE id=:i',i=b.own.item) is not None
    assert get_engine().pool.checkedout()==0


@pytest.mark.parametrize('field',['owner_user_id','item_id','file_id','version_id','missing','null'])
def test_sql_manifest_binding_independent_of_python_parser(deletion,monkeypatch,field):
    b=deletion;real=journal.next_record;before=b.w.path.read_bytes()
    def forged(head,kind,body):
        record=real(head,kind,body)
        if kind!=journal.PRIVATE_DELETION:return record
        body=copy.deepcopy(body)
        if field in ('owner_user_id','item_id'):body[field]=str(uuid.uuid4())
        elif field=='file_id':body['manifest']['files'][0]['id']=str(uuid.uuid4())
        elif field=='version_id':body['manifest']['files'][0]['version_id']=str(uuid.uuid4())
        elif field=='missing':body.pop('owner_user_id')
        else:body['owner_user_id']=None
        value={'body':body,'hash':record.hash,'kind':kind,'prev':record.prev,'seq':record.seq}
        return journal.Record(record.seq,kind,body,record.prev,record.hash,journal.canonical(value))
    monkeypatch.setattr(journal,'next_record',forged)
    r=remove(b)
    assert r.status_code==503,r.text
    assert b.w.path.read_bytes()==before
    assert scalar(b,'SELECT deleted_at FROM library_items WHERE id=:i',i=b.own.item) is None


def test_manifest_bound_rejects_whole_operation_before_append(deletion,monkeypatch):
    b=deletion
    with Session(b.w.engine) as db:db.add(PaperVersion(item_id=b.own.item));db.commit()
    monkeypatch.setattr(get_settings().retention,'private_delete_manifest_limit',1)
    before=b.w.path.read_bytes()
    response=remove(b)
    assert response.status_code==409 and response.json()['error']=='deletion_manifest_too_large'
    assert b.w.path.read_bytes()==before


def test_runtime_cannot_write_fence_or_raw_journal(deletion):
    b=deletion
    with b.w.runtime.connect() as c:
        assert not c.scalar(text("SELECT has_table_privilege('private_object_fences','INSERT')"))
        assert not c.scalar(text("SELECT has_table_privilege('revocation_journal_records','INSERT')"))
        assert c.scalar(text("SELECT has_function_privilege('ras_auth_private_cleanup_fence(uuid,text)','EXECUTE')"))
    with pytest.raises(DBAPIError),b.w.runtime.begin() as c:
        c.execute(text('SELECT ras_auth_private_cleanup_fence(:i,:k)'),{'i':uuid.uuid4(),'k':b.own.key})


def test_storage_failure_keeps_pending_and_next_tick_retries(deletion,monkeypatch):
    b=deletion;derived(b.w,b.own,'M6D-BODY');assert remove(b).status_code==200
    real=b.storage.delete
    monkeypatch.setattr(b.storage,'delete',lambda key:(_ for _ in ()).throw(OSError('injected storage failure')))
    assert purge(b)=={'error:storage_unavailable':1}
    assert scalar(b,'SELECT purged_at FROM private_item_deletions WHERE item_id=:i',i=b.own.item) is None
    assert scalar(b,'SELECT count(*) FROM chunks WHERE item_id=:i',i=b.own.item)>0
    assert scalar(b,'SELECT count(*) FROM private_object_fences')==1
    monkeypatch.setattr(b.storage,'delete',real)
    assert purge(b)=={'purged':1}
    assert scalar(b,'SELECT cleanup_attempts FROM private_item_deletions WHERE item_id=:i',i=b.own.item)==2


@pytest.mark.parametrize('phase',['prepare','finalize'])
@pytest.mark.parametrize('landed',[False,True])
def test_cleanup_commit_uncertain_stops_without_inference_and_later_tick_resumes(deletion,monkeypatch,phase,landed):
    b=deletion;assert remove(b).status_code==200;real=Session.commit;fired=[]
    def commit(db):
        if db.info.get('private_cleanup_phase')==phase and not fired:
            fired.append(True)
            if landed:real(db)
            raise OSError('injected cleanup commit uncertainty')
        return real(db)
    monkeypatch.setattr(Session,'commit',commit)
    assert purge(b)=={'halted:worker_commit_uncertain':1}
    assert b.storage.deleted==([] if phase=='prepare' else [b.own.key])
    monkeypatch.setattr(Session,'commit',real)
    assert purge(b)==({} if phase=='finalize' and landed else {'purged':1})


def test_recovery_private_storage_retains_zero_connections_including_operator_mutex(deletion,monkeypatch):
    b=deletion;assert remove(b).status_code==200;real=b.storage.delete;observed=[]
    def delete(key):
        observed.append(True)
        assert b.w.engine.pool.checkedout()==0
        assert b.w.runtime.pool.checkedout()==0
        assert get_engine().pool.checkedout()==0
        return real(key)
    monkeypatch.setattr(b.storage,'delete',delete)
    assert recovery.recover(b.w.engine,b.storage,get_settings(),b.clock()).safety_ready
    assert observed


@pytest.mark.parametrize('branch',['private','review'])
def test_delete_during_original_spool_denies_before_any_content(deletion,monkeypatch,branch):
    b=deletion
    from app.api.routes import uploads,reviews
    from app.main import app
    app.dependency_overrides[uploads._storage]=lambda:b.storage
    if branch=='review':opened(b)
    original=b.storage.open;fired=[]
    def read(key):
        result=original(key)
        if not fired:
            fired.append(True);direct(b)
        return result
    monkeypatch.setattr(b.storage,'open',read)
    if branch=='private':
        response=owner_get(b,f'/files/{b.own.file}/original')
    else:
        b.client.cookies.set('ras_session',b.w.actor.token)
        response=b.client.get(f'/admin/review-requests/{b.rid}/files/{b.own.file}/original')
    assert fired and response.status_code==404,response.text
    assert BODY.decode() not in response.text
    assert get_engine().pool.checkedout()==0


def test_delete_during_approval_copy_rolls_back_new_common_and_never_publishes(deletion):
    b=deletion;opened(b);fired=[]
    def during_copy(key):
        if not fired:
            fired.append(True);direct(b)
    b.storage.hooks['put']=during_copy
    response=approve(b,approval_body())
    assert fired and response.status_code in (404,409),response.text
    assert scalar(b,"SELECT count(*) FROM library_items WHERE visibility='common'")==0
    assert scalar(b,'SELECT count(*) FROM access_rights')==0
    assert all(key==b.own.key for key in b.storage.objects)


def test_citation_first_serializes_delete_and_no_late_links_after_commit(deletion,monkeypatch):
    b=deletion
    from app import citations
    from app.config import CitationPolicy
    from tests.test_c2d5f1_citation_runtime import refs
    from tests.test_c2d1_final_caller import wait_for
    refs(b,b.own,['arXiv:2609.00001'])
    entered,release=threading.Event(),threading.Event();real=citations._upsert
    def paused(*args,**kwargs):
        result=real(*args,**kwargs);entered.set();assert release.wait(20)
        return result
    monkeypatch.setattr(citations,'_upsert',paused)
    with ThreadPoolExecutor(2) as pool:
        citation=pool.submit(citations.reconcile_pending,b.w.runtime,CitationPolicy())
        assert entered.wait(10)
        deletion=pool.submit(direct,b)
        try:wait_for(b.w,'transactionid')
        finally:release.set()
        assert citation.result(timeout=20)['links_written']==1
        assert deletion.result(timeout=20)['deleted']
    assert citations.reconcile_pending(b.w.runtime,CitationPolicy()).get('links_written',0)==0
    assert purge(b)=={'purged':1}
    assert scalar(b,'SELECT count(*) FROM reference_citation_links')==0


def test_delete_first_discovery_retarget_does_not_rewrite_private_reference(deletion):
    b=deletion
    from app import citations,discovery
    from app.config import CitationPolicy,DiscoveryPolicy
    from tests.test_c2d5f1_citation_runtime import refs
    from tests.test_discovery import PAGE_ONE
    refs(b,b.own,['arXiv:2609.00001'])
    assert citations.reconcile_pending(b.w.runtime,CitationPolicy())['links_written']==1
    with b.w.engine.begin() as c:
        c.execute(text("INSERT INTO discovery_sources(topic,window_from) VALUES('cs.AI',:d)"),{'d':b.clock().date()})
    assert remove(b).status_code==200
    before=scalar(b,'SELECT match_status FROM reference_citation_links')
    result=discovery.run_sync(b.w.runtime,DiscoveryPolicy(topics=['cs.AI'],max_pages_per_topic=1),fetcher=lambda *_:PAGE_ONE)
    assert result['pages']==1
    assert scalar(b,'SELECT match_status FROM reference_citation_links')==before


@pytest.mark.parametrize('mutation',['extra','null_owner','negative_epoch','bool_epoch','file_other_version',
    'missing_checksum','duplicate_file','duplicate_version','foreign_key','bad_deadline'])
def test_journal_parser_rejects_incomplete_or_ambiguous_private_manifest(mutation):
    owner,item,ident,version,file=(str(uuid.uuid4()) for _ in range(5))
    body=dict(id=ident,item_id=item,owner_user_id=owner,rights_epoch=1,deleted_at='2026-10-16T00:00:00+00:00',
        purge_due_at='2026-10-17T00:00:00+00:00',manifest={'versions':[version],'files':[
            dict(id=file,version_id=version,job_id=None,storage_key='originals/'+uuid.uuid4().hex,checksum_sha256='a'*64)]})
    if mutation=='extra':body['title']='PRIVATE-TITLE'
    elif mutation=='null_owner':body['owner_user_id']=None
    elif mutation=='negative_epoch':body['rights_epoch']=-1
    elif mutation=='bool_epoch':body['rights_epoch']=True
    elif mutation=='file_other_version':body['manifest']['files'][0]['version_id']=str(uuid.uuid4())
    elif mutation=='missing_checksum':body['manifest']['files'][0].pop('checksum_sha256')
    elif mutation=='duplicate_file':body['manifest']['files']*=2
    elif mutation=='duplicate_version':body['manifest']['versions']*=2
    elif mutation=='foreign_key':body['manifest']['files'][0]['storage_key']='private/title-containing-key'
    else:body['purge_due_at']='2026-10-15T00:00:00+00:00'
    with pytest.raises(journal.JournalError):journal.make_record(2,journal.PRIVATE_DELETION,body,journal.ZERO_HASH)


@pytest.mark.parametrize('mode',['none','prefix','full_fault','success'])
def test_final_session_expiry_after_every_append_result_rolls_back_then_replay_only_restricts(deletion,monkeypatch,mode):
    b=deletion;opened(b);original=journal.append
    def append(path,state,records):
        if mode!='none':original(path,state,records[:1] if mode=='prefix' else records)
        b.at+=get_settings().auth.session_idle+timedelta(seconds=1)
        if mode!='success':raise OSError('injected append/fsync fault after session expiry')
    monkeypatch.setattr(journal,'append',append)
    outcome=boundary.Outcome()
    with pytest.raises(ApiError) as error:direct(b,outcome)
    assert error.value.status_code==503 and error.value.code=='deletion_pending_recovery'
    assert outcome.append_started and not outcome.commit_started and outcome.rollback_confirmed
    assert scalar(b,'SELECT deleted_at FROM library_items WHERE id=:i',i=b.own.item) is None
    assert scalar(b,'SELECT count(*) FROM private_item_deletions')==0
    assert scalar(b,"SELECT status FROM review_requests WHERE id=:r",r=b.rid)=='open'
    monkeypatch.setattr(journal,'append',original)
    assert recovery.recover(b.w.engine,b.storage,get_settings(),b.clock()).safety_ready
    assert (scalar(b,'SELECT deleted_at IS NOT NULL FROM library_items WHERE id=:i',i=b.own.item))==(mode in ('full_fault','success'))
    assert scalar(b,"SELECT status FROM review_requests WHERE id=:r",r=b.rid)==('open' if mode=='none' else 'closed_by_recovery')


def test_response_session_expiry_after_known_commit_is_unknown_to_client_without_resend(deletion,monkeypatch):
    b=deletion;original=boundary.respond
    def respond(*args,**kwargs):
        b.at+=get_settings().auth.session_idle+timedelta(seconds=1)
        return original(*args,**kwargs)
    monkeypatch.setattr(boundary,'respond',respond)
    outcome=boundary.Outcome()
    with pytest.raises(ApiError) as error:direct(b,outcome)
    assert error.value.status_code==503 and error.value.code=='deletion_response_unavailable'
    assert outcome.committed
    assert scalar(b,'SELECT deleted_at IS NOT NULL FROM library_items WHERE id=:i',i=b.own.item)
    assert scalar(b,'SELECT count(*) FROM private_item_deletions')==1
    assert recovery.evaluate(get_settings(),b.w.runtime).open
