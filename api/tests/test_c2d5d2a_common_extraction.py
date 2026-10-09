"""Common extraction through runtime rights; actor session is attribution only."""
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import publication
from app.config import get_settings
from app.db import get_engine
from app.identity.sessions import revoke_session
from app.identity.session_commit import commit_auth
from app.ingest import worker, checkpoints
from app.models.enums import SessionRevokeReason
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, wait_for  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes, index, jobs
from tests.resource_fixtures import runtime_revoke, BODY
from tests.test_auth_safety_foundation import NOW


@pytest.fixture
def common(indexes, monkeypatch):
    b=indexes
    assert index(b).status_code==200
    b.job=jobs(b)[0]['id']
    monkeypatch.setattr(worker,'utcnow',lambda:NOW)
    b.opens=[]
    original=b.raw_storage.open
    def opened(key):
        assert get_engine().pool.checkedout()==0
        b.opens.append(key)
        return original(key)
    monkeypatch.setattr(b.raw_storage,'open',opened)
    return b


def count(b,table):
    with b.w.engine.connect() as c:
        return c.scalar(text(f'SELECT count(*) FROM {table} WHERE item_id=:i'),{'i':b.copy.item})


def test_common_logout_actor_still_extracts_separate_from_private(common):
    b=common
    from tests.test_c2d5d2a_process_smoke import attach
    private_job=attach(b.w,b.own,b.user.id)
    b.raw_storage.put(b.own.key,io.BytesIO(BODY),'text/markdown')
    assert worker.process_job(private_job,storage=b.raw_storage)=='awaiting_index'
    with b.w.engine.connect() as c:
        before=c.execute(text('SELECT * FROM chunks WHERE item_id=:i ORDER BY id'),{'i':b.own.item}).all()
    b.opens.clear()
    with Session(b.w.runtime) as db:
        revoke_session(db,session_id=b.w.actor.session_id,user_id=b.w.uid,now=NOW,reason=SessionRevokeReason.LOGOUT)
        commit_auth(db)
    assert worker.process_job(b.job,storage=b.raw_storage)=='awaiting_index'
    assert count(b,'sections') and count(b,'chunks')
    assert len(b.opens)==3
    with b.w.engine.connect() as c:
        job=c.execute(text('SELECT state,finished_at,extraction_finished_at,lease_token FROM ingestion_jobs WHERE id=:j'),{'j':b.job}).one()
        item=c.execute(text('SELECT content_status,active_index_generation_id FROM library_items WHERE id=:i'),{'i':b.copy.item}).one()
        private=c.scalar(text('SELECT count(*) FROM chunks WHERE item_id=:i'),{'i':b.own.item})
        assert job[0]=='awaiting_index' and job[1] is None and job[2] is not None and job[3] is None
        assert item==('metadata_only',None) and private>0
        assert c.execute(text('SELECT * FROM chunks WHERE item_id=:i ORDER BY id'),{'i':b.own.item}).all()==before
        assert c.scalar(text('SELECT count(*) FROM embedding_calls'))==0


@pytest.mark.parametrize('phase',['storage','parser'])
def test_common_revoke_commits_while_io_blocked_and_no_result(common,monkeypatch,phase):
    b=common; entered,release=threading.Event(),threading.Event()
    original_open,original_extract=b.raw_storage.open,worker.extract
    def opened(key):
        if len(b.opens)==1:
            entered.set(); assert release.wait(20)
        return original_open(key)
    def parsed(original):
        entered.set(); assert release.wait(20)
        return original_extract(original)
    monkeypatch.setattr(b.raw_storage,'open',opened if phase=='storage' else original_open)
    if phase=='parser':monkeypatch.setattr(worker,'extract',parsed)
    with ThreadPoolExecutor(2) as pool:
        attempt=pool.submit(worker.process_job,b.job,None,b.raw_storage)
        assert entered.wait(10)
        try:
            result=pool.submit(runtime_revoke,b.w,b.copy.item,clock=b.clock).result(10)
            assert result['item']['publication_state']=='revoked'
        finally:release.set()
        assert attempt.result(15)=='cancelled'
    assert count(b,'sections')==count(b,'chunks')==0
    assert len(b.opens)==2 and jobs(b)[0]['cancel_requested']


@pytest.mark.parametrize('first',['claim','revoke'])
def test_claim_revoke_order_business_result_no_deadlock(common,monkeypatch,first):
    b=common; entered,release=threading.Event(),threading.Event()
    if first=='claim':
        original=checkpoints.lock_item_then_job
        def hold(*args,**kwargs):
            rows=original(*args,**kwargs)
            entered.set(); assert release.wait(15)
            return rows
        monkeypatch.setattr(checkpoints,'lock_item_then_job',hold)
    else:
        original=publication._cancel_jobs
        def hold(*args,**kwargs):
            entered.set(); assert release.wait(15)
            return original(*args,**kwargs)
        monkeypatch.setattr(publication,'_cancel_jobs',hold)
    def claim():
        with Session(b.w.runtime) as db:return worker.claim_job(db,b.job,get_settings())
    def revoke():return runtime_revoke(b.w,b.copy.item,clock=b.clock)
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(claim if first=='claim' else revoke)
        assert entered.wait(10)
        two=pool.submit(revoke if first=='claim' else claim)
        try:
            if first=='claim':assert wait_for(b.w.engine,'transactionid')
            else:assert two.result(5) is None  # SKIP LOCKED item, not job-before-item wait
        finally:release.set()
        a,z=one.result(20),two.result(20)
    assert (z if first=='claim' else a)['item']['publication_state']=='revoked'
    assert jobs(b)[0]['attempt_count']==(1 if first=='claim' else 0)
    assert jobs(b)[0]['cancel_requested'] and not b.opens
    assert count(b,'sections')==count(b,'chunks')==0


@pytest.mark.parametrize('change',['fulltext','external','conditions','epoch','checksum','version'])
def test_exact_common_gate_before_io(common,change):
    b=common
    statements={
        'fulltext':"UPDATE access_rights SET permits_fulltext_search=false,permits_external_processing=false WHERE item_id=:i",
        'external':"UPDATE access_rights SET permits_external_processing=false WHERE item_id=:i",
        'conditions':"UPDATE access_rights SET scope_conditions='unimplemented' WHERE item_id=:i",
        'epoch':"UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i",
        'checksum':"UPDATE source_files SET checksum_sha256=repeat('0',64) WHERE item_id=:i",
        'version':"UPDATE ingestion_jobs SET version_id=NULL WHERE item_id=:i"}
    # Labelled owner/adversary corruption for states production triggers forbid.
    with b.w.engine.begin() as c:
        for table in ('library_items','access_rights','source_files','ingestion_jobs'):
            c.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER ALL'))
        if change=='version':
            # Keep CHECK/FK valid while corrupting exact job-version binding.
            v=c.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'adversary') RETURNING id"),{'i':b.copy.item})
            c.execute(text('UPDATE ingestion_jobs SET version_id=:v WHERE id=:j'),{'v':v,'j':b.job})
            # Composite source-file FK is also a trigger; user triggers include FK on PG.
        else:c.execute(text(statements[change]),{'i':b.copy.item})
        for table in ('library_items','access_rights','source_files','ingestion_jobs'):
            c.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER ALL'))
    result=worker.process_job(b.job,storage=b.raw_storage)
    assert result in ('common_fulltext_not_permitted','common_external_processing_not_permitted',
        'common_blocked_by_conditions','cancelled','common_rights_not_current','invalid_job_file_relation')
    assert not b.opens and count(b,'sections')==0
    assert jobs(b)[0]['attempt_count']==0


def test_runtime_extraction_rows_keep_fence_and_system_cleanup(common):
    b=common
    assert worker.process_job(b.job,storage=b.raw_storage)=='awaiting_index'
    assert count(b,'sections') and count(b,'chunks')
    runtime_revoke(b.w,b.copy.item,clock=b.clock)
    from app.models import MetadataCandidate
    from sqlalchemy.exc import DBAPIError
    with Session(b.w.runtime) as db, pytest.raises(DBAPIError):
        db.add(MetadataCandidate(item_id=b.copy.item,version_id=b.copy.version,source_file_id=b.copy.file,
            field_name='title',value='late',locator_kind='markdown',start_offset=0,end_offset=4,pipeline_version='synthetic'))
        db.flush()
    # Labelled legacy/system cleanup regression; this does NOT convert cleanup.
    assert publication.cleanup_revoked(b.w.engine,b.raw_storage,NOW+timedelta(days=30),10)=={'deleted':1}
    with b.w.engine.connect() as c:
        assert c.scalar(text('SELECT ras_common_copy_derived_left(:i)'),{'i':b.copy.item})==[]
        assert c.scalar(text('SELECT count(*) FROM source_files WHERE item_id=:i'),{'i':b.own.item})==1
    assert count(b,'sections')==count(b,'chunks')==0
