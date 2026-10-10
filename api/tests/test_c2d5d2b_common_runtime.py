"""Common document runtime: resource rights outlive actor session, never revoke."""
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import publication, auth_safety
from app.config import get_settings
from app.db import get_engine
from app.ingest import document_index as c, document_accounting as money, indexer as ix, worker as w, runtime
from app.ingest.embedding import EmbeddingResult
from app.identity.sessions import revoke_session
from app.identity.session_commit import commit_auth
from app.models.enums import SessionRevokeReason
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers, wait_for  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes, index, jobs  # noqa: F401
from tests.test_c2d5d2a_common_extraction import common, count  # noqa: F401
from tests.resource_fixtures import runtime_revoke, BODY
from tests.test_auth_safety_foundation import NOW


@pytest.fixture
def doc(common,monkeypatch):
    b=common
    assert w.process_job(b.job,storage=b.raw_storage)=='awaiting_index'
    b.opens.clear()
    b.settings=get_settings().model_copy(deep=True)
    b.settings.embedding.api_key=SecretStr('fake-common-document-transport')
    b.settings.embedding.pricing_verified=True  # this test copy ONLY
    b.calls=[]; b.action=None
    client=httpx.Client
    def handle(request):
        assert get_engine().pool.checkedout()==0
        payload=json.loads(request.content)
        assert 'References' not in payload['input']
        b.calls.append(payload['input'])
        if b.action: b.action()
        return httpx.Response(200,json={'model':'text-embedding-3-small','provider':'Azure',
            'data':[{'embedding':[.1,.2,.3]}],'usage':{'prompt_tokens':3}})
    def fake(*a,**kw):
        kw['transport']=httpx.MockTransport(handle)
        return client(*a,**kw)
    monkeypatch.setattr(httpx,'Client',fake)
    return b


def sql(b,q,**params):
    with b.w.engine.begin() as con:
        r=con.execute(text(q),params)
        return r.scalar() if r.returns_rows else None


def run(b): return w.process_job(b.job,b.settings,b.raw_storage)


def test_common_uses_application_buckets_after_actor_logout(doc):
    b=doc
    with Session(b.w.runtime) as db:
        revoke_session(db,session_id=b.w.actor.session_id,user_id=b.w.uid,now=NOW,reason=SessionRevokeReason.LOGOUT)
        commit_auth(db)
    assert run(b)=='ready' and b.calls
    assert sql(b,'SELECT count(*) FROM embedding_calls WHERE owner_user_id IS NOT NULL')==0
    assert sql(b,'SELECT count(*) FROM embedding_user_budget_days')==0
    assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0
    assert sql(b,'SELECT count(*) FROM index_entries')>0
    assert sql(b,"SELECT state FROM ingestion_jobs WHERE id=:i",i=b.job)=='ready'


@pytest.mark.parametrize('change',['fulltext','external','conditions','epoch','checksum','version'])
@pytest.mark.parametrize('phase',['hash','provider'])
def test_common_exact_gate_changes_at_io(doc,monkeypatch,change,phase):
    b=doc
    def alter():
        statements={
            'fulltext':"UPDATE access_rights SET permits_fulltext_search=false,permits_external_processing=false WHERE item_id=:i",
            'external':"UPDATE access_rights SET permits_external_processing=false WHERE item_id=:i",
            'conditions':"UPDATE access_rights SET scope_conditions='unimplemented' WHERE item_id=:i",
            'epoch':"UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i",
            'checksum':"UPDATE source_files SET checksum_sha256=repeat('0',64) WHERE item_id=:i",
            'audience':"UPDATE access_rights SET scope_audience='owner_only' WHERE item_id=:i"}
        with b.w.engine.begin() as con:  # labelled adversarial owner corruption
            for table in ('library_items','access_rights','source_files','ingestion_jobs'):
                con.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER ALL'))
            if change=='version':
                vid=con.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'adversary') RETURNING id"),{'i':b.copy.item})
                con.execute(text('UPDATE ingestion_jobs SET version_id=:v WHERE id=:j'),{'v':vid,'j':b.job})
            else: con.execute(text(statements[change]),{'i':b.copy.item})
            for table in ('library_items','access_rights','source_files','ingestion_jobs'):
                con.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER ALL'))
    if phase=='provider': b.action=alter
    else:
        original=b.raw_storage.open
        def opened(key):
            stream=original(key); alter(); return stream
        monkeypatch.setattr(b.raw_storage,'open',opened)
    expected={'fulltext':'common_fulltext_not_permitted','external':'common_external_processing_not_permitted',
        'conditions':'common_blocked_by_conditions','epoch':'cancelled','checksum':'common_rights_not_current',
        'version':'invalid_job_file_relation','audience':'common_audience_not_supported'}[change]
    assert run(b)==expected
    assert count(b,'index_generations')==(1 if phase=='provider' else 0)
    assert sql(b,'SELECT count(*) FROM index_entries')==0
    assert sql(b,'SELECT count(*) FROM embedding_calls WHERE vector IS NOT NULL')==0
    if phase=='provider':
        assert len(b.calls)==1 and sql(b,"SELECT count(*) FROM embedding_calls WHERE status='completed'")==1
        assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0
    else: assert not b.calls


@pytest.mark.parametrize('phase',['hash','provider'])
@pytest.mark.parametrize('cleanup',[False,True])
def test_revoke_commits_during_unlocked_io_and_late_result_financial_only(doc,monkeypatch,phase,cleanup):
    b=doc; entered,release=threading.Event(),threading.Event()
    def paused(): entered.set(); assert release.wait(30)
    if phase=='provider': b.action=paused
    else:
        original=b.raw_storage.open
        def opened(key):
            stream=original(key); paused(); return stream
        monkeypatch.setattr(b.raw_storage,'open',opened)
    with ThreadPoolExecutor(2) as pool:
        running=pool.submit(run,b)
        assert entered.wait(15)
        try:
            revoked=pool.submit(runtime_revoke,b.w,b.copy.item,clock=b.clock)
            assert revoked.result(15)['item']['publication_state']=='revoked'
            if cleanup:
                assert publication.operator_cleanup_revoked(b.w.engine,b.raw_storage,NOW+timedelta(days=30),10)=={'deleted':1}
        finally: release.set()
        assert running.result(20) in ('cancelled','checksum_mismatch','lease_lost')
    assert sql(b,'SELECT count(*) FROM index_entries')==0
    assert sql(b,'SELECT count(*) FROM embedding_calls WHERE vector IS NOT NULL')==0
    assert sql(b,'SELECT active_index_generation_id FROM library_items WHERE id=:i',i=b.copy.item) is None
    if phase=='provider':
        assert sql(b,"SELECT status FROM embedding_calls")=='completed'
        assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0
        if cleanup: assert sql(b,'SELECT chunk_hash IS NULL AND content_removed_at IS NOT NULL FROM embedding_calls')
    else: assert not b.calls
    assert sql(b,'SELECT count(*) FROM source_files WHERE item_id=:i',i=b.own.item)==1


@pytest.mark.parametrize('first',['claim','revoke'])
def test_index_claim_and_revoke_both_orders(doc,monkeypatch,first):
    b=doc; entered,release=threading.Event(),threading.Event()
    from app.ingest import checkpoints
    if first=='claim':
        original=checkpoints.lock_item_then_job
        def hold(*a,**kw):
            rows=original(*a,**kw); entered.set(); assert release.wait(20); return rows
        monkeypatch.setattr(checkpoints,'lock_item_then_job',hold)
    else:
        original=publication._cancel_jobs
        def hold(*a,**kw):
            entered.set(); assert release.wait(20); return original(*a,**kw)
        monkeypatch.setattr(publication,'_cancel_jobs',hold)
    def claim():
        with Session(b.w.runtime) as db: return c.claim(db,b.job,b.settings)
    def revoke(): return runtime_revoke(b.w,b.copy.item,clock=b.clock)
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(claim if first=='claim' else revoke)
        assert entered.wait(15)
        two=pool.submit(revoke if first=='claim' else claim)
        try:
            if first=='claim': assert wait_for(b.w.engine,'transactionid')
            else: assert two.result(10) is None
        finally: release.set()
        a,z=one.result(20),two.result(20)
    assert (z if first=='claim' else a)['item']['publication_state']=='revoked'
    assert not b.calls and not b.opens
    assert jobs(b)[0]['cancel_requested']
    assert jobs(b)[0]['attempt_count']==(2 if first=='claim' else 1)


@pytest.mark.parametrize('first',['publish','revoke'])
def test_publish_and_revoke_both_orders(doc,monkeypatch,first):
    b=doc; entered,release=threading.Event(),threading.Event()
    original=ix._publish
    if first=='publish':
        def held(*a,**kw):
            original(*a,**kw); entered.set(); assert release.wait(20)
        monkeypatch.setattr(ix,'_publish',held)
    else:
        # Revoke while the mandatory final hash is outside every DB lock.
        original_hash=ix._verify_object
        def held(source,storage,settings):
            value=original_hash(source,storage,settings)
            if sql(b,'SELECT count(*) FROM index_entries')==sql(b,"SELECT count(*) FROM chunks c JOIN sections s ON s.id=c.section_id WHERE s.kind IN ('body','abstract')"):
                entered.set(); assert release.wait(20)
            return value
        monkeypatch.setattr(ix,'_verify_object',held)
    with ThreadPoolExecutor(2) as pool:
        running=pool.submit(run,b)
        assert entered.wait(15)
        revoking=pool.submit(runtime_revoke,b.w,b.copy.item,clock=b.clock)
        try:
            if first=='publish': assert wait_for(b.w.engine,'transactionid')
            else: assert revoking.result(15)['item']['publication_state']=='revoked'
        finally: release.set()
        code=running.result(20)
        assert revoking.result(20)['item']['publication_state']=='revoked'
    assert code==('ready' if first=='publish' else 'cancelled')
    assert sql(b,'SELECT active_index_generation_id FROM library_items WHERE id=:i',i=b.copy.item) is None
    assert sql(b,'SELECT count(*) FROM embedding_calls WHERE owner_user_id IS NOT NULL')==0
