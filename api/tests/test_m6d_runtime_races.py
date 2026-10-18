"""Deterministic deletion at existing storage/provider/publication barriers."""
import io
import uuid
import pytest
from sqlalchemy import text
from app import private_cleanup
from app.config import get_settings
from app.db import get_engine
from app.ingest import worker, document_index
from tests.test_c2c1_bootstrap import installation  # noqa
from tests.test_uploads import upload_api  # noqa
from tests.test_c2d5d2a_worker_runtime import work,sql,BODY  # noqa
from tests.test_c2d5d2b_document_runtime import indexed,run  # noqa
from tests.test_c2d6_search_runtime import ready,request as search_request  # noqa
from tests.test_c2d7_rag_runtime import generated,ask  # noqa
from tests.test_ingest_worker import upload


@pytest.mark.parametrize('state',['received','awaiting_index','needs_review'])
def test_delete_each_unfinished_ingest_state_closes_job_and_blocks_retry(work,state):
    b=work
    if state=='awaiting_index':assert worker.process_job(b.job,storage=b.storage)=='awaiting_index'
    elif state=='needs_review':
        from tests.test_pdf_ingest import upload_pdf,pdf_bytes
        ids=upload_pdf(b.browser,pdf_bytes([[]]))
        b.job,b.item,b.file=(uuid.UUID(ids[k]) for k in ('job_id','item_id','file_id'))
        assert worker.process_job(b.job,storage=b.storage)=='needs_review'
    assert sql(b,'SELECT state FROM ingestion_jobs WHERE id=:j',j=b.job)==state
    remove(b)
    assert sql(b,'SELECT cancel_requested AND lease_token IS NULL FROM ingestion_jobs WHERE id=:j',j=b.job)
    assert worker.process_job(b.job,storage=b.storage) in ('not_claimed','cancelled')
    assert b.browser.get(f'/files/{b.file}/original').status_code==404


def remove(b):
    response=b.browser.post(f'/library/items/{b.item}/delete',{'confirm_delete':True})
    assert response.status_code==200,response.text
    assert get_engine().pool.checkedout()==0


def purge(b):
    result=private_cleanup.cleanup_private(b.api.engine,b.storage,get_settings(),clock=lambda:b.api.clock.now)
    assert result=={'purged':1},result


def test_delete_during_extraction_storage_prevents_any_derived_commit(work,monkeypatch):
    b=work; original=b.storage.open; fired=[]
    def opened(key):
        result=original(key)
        if not fired:
            fired.append(True);remove(b)
        return result
    monkeypatch.setattr(b.storage,'open',opened)
    result=worker.process_job(b.job,storage=b.storage)
    assert result in ('cancelled','lease_lost'),result
    assert sql(b,'SELECT count(*) FROM chunks WHERE item_id=:i',i=b.item)==0
    assert sql(b,'SELECT active_index_generation_id FROM library_items WHERE id=:i',i=b.item) is None


def test_delete_during_document_provider_settles_once_without_content(indexed):
    b=indexed; fired=[]
    def action(request):
        if not fired:
            fired.append(True);remove(b);purge(b)
    b.action=action
    result=run(b)
    assert result not in ('ready','awaiting_index'),result
    assert len(b.calls)==1
    assert sql(b,"SELECT count(*) FROM embedding_calls WHERE status='completed'")==1
    assert sql(b,'SELECT count(*) FROM embedding_calls WHERE vector IS NOT NULL OR chunk_hash IS NOT NULL')==0
    assert sql(b,'SELECT count(*) FROM index_entries')==0
    assert sql(b,'SELECT active_index_generation_id FROM library_items WHERE id=:i',i=b.item) is None


def test_delete_immediately_before_index_activate(indexed,monkeypatch):
    b=indexed; original=document_index.step; fired=[]
    def step(*args,**kwargs):
        if kwargs.get('publish') and not fired:
            fired.append(True);remove(b)
        return original(*args,**kwargs)
    monkeypatch.setattr(document_index,'step',step)
    result=run(b)
    assert fired and result!='ready'
    assert sql(b,'SELECT active_index_generation_id FROM library_items WHERE id=:i',i=b.item) is None


@pytest.mark.parametrize('scope',['item','all','collection'])
def test_delete_during_rerank_never_returns_paper(ready,scope):
    b=ready; body={}
    if scope=='item':body['item_id']=str(b.item)
    elif scope=='collection':
        collection=b.browser.post('/collections',{'name':'M6d race'}).json()['collection']['id']
        assert b.browser.post(f'/collections/{collection}/items',{'item_id':str(b.item)}).status_code==201
        body['collection_id']=collection
    fired=[]
    def action(request,payload):
        if 'documents' in payload and not fired:
            fired.append(True);remove(b)
    b.action=action
    response=search_request(b,mode='keyword',rerank=True,**body)
    assert fired and response.status_code==409,response.text
    assert 'measured' not in response.text
    assert len(b.calls)==1


@pytest.mark.parametrize('scope',['item','all','collection'])
def test_delete_during_generation_never_returns_answer_or_citation(generated,scope):
    b=generated; body={'scope':'all' if scope=='all' else None}
    if scope=='item':body['item_id']=str(b.item)
    elif scope=='collection':
        collection=b.browser.post('/collections',{'name':'M6d generation'}).json()['collection']['id']
        assert b.browser.post(f'/collections/{collection}/items',{'item_id':str(b.item)}).status_code==201
        body['collection_id']=collection
    fired=[]
    def action(request,payload):
        if not fired:fired.append(True);remove(b)
    b.gen_action=action
    response=ask(b,mode='keyword',**body)
    assert fired and response.status_code==409,response.text
    assert 'claims' not in response.text and 'measured' not in response.text
    assert sql(b,"SELECT count(*) FROM search_usage WHERE task_type='generation' AND status='completed'")==1


def test_deleted_item_not_retrieved_before_top_k_and_new_upload_is_independent(ready):
    b=ready; b.settings.search.keyword_top_k=1; b.settings.search.vector_top_k=1
    first=search_request(b,mode='keyword',rerank=False)
    assert first.status_code==200 and first.json()['hits']
    saved=first.json()['hits'][0]
    remove(b)
    assert b.browser.get('/search/chunks/'+saved['chunk_id']).status_code==404
    assert search_request(b,mode='keyword',rerank=False).json()['hits']==[]
    fresh=upload(b.browser,BODY)
    assert fresh['item_id']!=str(b.item)
    # Use the same real document adapter with the controlled transport.
    assert worker.process_job(uuid.UUID(fresh['job_id']),b.settings,b.storage)=='awaiting_index'
    assert worker.process_job(uuid.UUID(fresh['job_id']),b.settings,b.storage)=='ready'
    response=search_request(b,mode='keyword',rerank=False)
    assert response.status_code==200 and len(response.json()['hits'])==1,response.text
    assert response.json()['hits'][0]['item_id']==fresh['item_id']
    assert str(b.item) not in response.text
