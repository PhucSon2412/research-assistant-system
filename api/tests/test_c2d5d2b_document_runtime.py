"""Real runtime/PG/MinIO/journal; fake HTTP transport through the real adapter."""
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app import auth_safety, recovery
from app.config import get_settings
from app.db import get_engine
from app.ingest import worker as w, indexer as ix, document_index as c, document_accounting as money, runtime
from app.ingest.embedding import EmbeddingResult, EmbeddingError
from app.models import EmbeddingCall
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_uploads import upload_api  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work, sql, owner_status, BODY  # noqa: F401
from tests.test_ingest_worker import upload
from tests.test_pdf_ingest import full_pdf, upload_pdf
from tests.test_auth_safety_foundation import NOW


@pytest.fixture
def indexed(work, monkeypatch):
    b = work
    assert w.process_job(b.job, storage=b.storage) == 'awaiting_index'
    b.opened.clear()
    b.expected=sql(b,"SELECT count(DISTINCT c.chunk_hash) FROM chunks c JOIN sections s ON s.id=c.section_id WHERE c.item_id=:i AND s.kind IN ('body','abstract')",i=b.item)
    b.settings = get_settings().model_copy(deep=True)
    b.settings.embedding.api_key = SecretStr('fake-document-transport-only')
    b.settings.embedding.pricing_verified = True  # ONLY this copy, attached to MockTransport below
    b.calls, b.action = [], None
    client = httpx.Client
    def handle(request):
        assert get_engine().pool.checkedout() == 0, 'connection retained during provider IO'
        payload = json.loads(request.content)
        assert payload['input_type'] == 'search_document'
        assert payload['model'] == b.settings.embedding.model_id
        assert 'References' not in payload['input'] and 'arXiv:' not in payload['input']
        b.calls.append(payload['input'])
        if b.action:
            response = b.action(request)
            if response is not None:
                return response
        return httpx.Response(200, json={'model':'text-embedding-3-small','provider':'Azure',
            'data':[{'embedding':[0.1, 0.2, 0.3]}], 'usage':{'prompt_tokens':3}})
    def fake_client(*a, **kw):
        kw['transport'] = httpx.MockTransport(handle)
        return client(*a, **kw)
    monkeypatch.setattr(httpx, 'Client', fake_client)
    return b


def run(b):
    return w.process_job(b.job, b.settings, b.storage)


def prepare(b):
    db = Session(b.api.engine, expire_on_commit=False)
    token = c.claim(db, b.job, b.settings)
    assert token
    source = c.proof(db, b.job, token, b.settings)
    verified = ix._verify_object(source, b.storage, b.settings)
    admission = c.step(db, b.job, token, b.settings, source, verified)
    assert admission.send
    return db, token, admission, verified


def result(b):
    return EmbeddingResult([.1,.2,.3], b.settings.embedding.model_id, 3, 'text-embedding-3-small', 'Azure')


def test_import_direct_entry_and_atomic_publish(indexed):
    b = indexed
    assert run(b) == 'ready'
    assert len(b.calls) == b.expected and len(b.opened) >= 2
    assert sql(b,"SELECT count(*) FROM index_entries WHERE keywords @@ plainto_tsquery('simple','measured')") == 1
    assert sql(b,"SELECT count(*) FROM embedding_calls WHERE status='completed' AND vector_dims(vector)=3 AND reservation_token IS NOT NULL") == b.expected
    assert sql(b,"SELECT count(*) FROM library_items i JOIN ingestion_jobs j ON j.item_id=i.id JOIN index_generations g ON g.id=i.active_index_generation_id WHERE i.content_status='indexed' AND g.state='active' AND j.state='ready' AND j.finished_at IS NOT NULL AND j.lease_token IS NULL") == 1
    assert sql(b,"SELECT reserved_usd FROM embedding_budget_days") == 0
    assert run(b) == 'not_claimed'
    with Session(b.api.engine) as db:
        with pytest.raises(runtime.Halt, match='lease_lost'):
            ix.process_index(db,b.job,uuid.uuid4(),b.storage,b.settings)


@pytest.mark.parametrize('change', ['keyword_text', 'keyword_hash', 'call_provider', 'generation_provider'])
def test_publish_rechecks_keyword_and_provider_binding(indexed, monkeypatch, change):
    b = indexed
    original = ix._publish
    def corrupt(db, job, item, generation, chunks, settings):
        # Labelled adversarial mutation inside the transaction, before the pure
        # publication validator. A failed publish must roll this mutation back.
        statements = {
            'keyword_text': "UPDATE index_entries SET text=text||' forged' WHERE generation_id=:i",
            'keyword_hash': "UPDATE index_entries SET chunk_hash=repeat('0',64) WHERE generation_id=:i",
            'call_provider': "UPDATE embedding_calls SET provider='wrong' WHERE id IN (SELECT embedding_call_id FROM index_entries WHERE generation_id=:i)",
            'generation_provider': "UPDATE index_generations SET provider='wrong' WHERE id=:i"}
        db.execute(text(statements[change]), {'i': generation.id})
        db.expire_all()
        return original(db, job, item, generation, chunks, settings)
    monkeypatch.setattr(ix, '_publish', corrupt)
    assert run(b) == ('index_keyword_mismatch' if change.startswith('keyword')
        else 'index_generation_changed' if change == 'generation_provider' else 'index_vector_mismatch')
    assert sql(b, 'SELECT count(*) FROM index_generations WHERE state=\'active\'') == 0
    assert sql(b, 'SELECT active_index_generation_id IS NULL FROM library_items WHERE id=:i', i=b.item)
    assert len(b.calls) == b.expected
    assert sql(b, 'SELECT reserved_usd FROM embedding_budget_days') == 0


@pytest.mark.parametrize('state,resume',[('awaiting_index',None),('indexing',None),('retryable_failed','indexing')])
@pytest.mark.parametrize('missing',['key','price'])
def test_config_pause_does_not_claim_or_reserve(indexed,state,resume,missing):
    b=indexed
    sql(b,'UPDATE ingestion_jobs SET state=:s,resume_state=:r WHERE id=:i',s=state,r=resume,i=b.job)
    if missing=='key': b.settings.embedding.api_key=None
    else: b.settings.embedding.pricing_verified=False
    before=sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)
    assert run(b)==('embedding_key_missing' if missing=='key' else 'embedding_pricing_unverified')
    with Session(b.api.engine) as db: assert b.job not in w.due_job_ids(db,b.settings)
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==before
    assert not b.opened and not b.calls and sql(b,'SELECT count(*) FROM embedding_calls')==0


@pytest.mark.parametrize('deadline', ['expired', 'absent', 'live'])
def test_document_capacity_counts_only_live_query_leases(indexed, deadline):
    b=indexed; b.settings.embedding.max_concurrent_requests=1
    at=w.utcnow()
    until=at+timedelta(minutes=5) if deadline=='live' else at-timedelta(seconds=1) if deadline=='expired' else None
    query=sql(b, '''INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,
        reserved_usd,cost_status,lease_until) VALUES (:u,:d,'embed_query',:m,:p,'reserved',0.01,'unknown',:t) RETURNING id''',
        u=b.uid,d=at.date(),m=b.settings.embedding.model_id,p=b.settings.embedding.provider,t=until)
    assert run(b)==('embedding_concurrency_limit' if deadline=='live' else 'ready')
    assert len(b.calls)==(0 if deadline=='live' else b.expected)
    # Query accounting belongs to its existing boundary. A dead slot does not
    # erase its financial ceiling or change its status in document admission.
    assert sql(b,'SELECT reserved_usd FROM search_usage WHERE id=:i',i=query)==Decimal('0.01')
    assert sql(b,"SELECT status FROM search_usage WHERE id=:i",i=query)=='reserved'
    assert sql(b,'SELECT cost_usd IS NULL FROM search_usage WHERE id=:i',i=query)


def test_expired_query_still_counts_towards_document_budget(indexed):
    b=indexed; b.settings.embedding.max_concurrent_requests=1
    b.settings.budget.embedding_daily_usd=0.01
    b.settings.budget.user_embedding_daily_usd=0.01
    at=w.utcnow()
    sql(b, '''INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,
        reserved_usd,cost_status,lease_until) VALUES (:u,:d,'embed_query',:m,:p,'reserved',0.01,'unknown',:t)''',
        u=b.uid,d=at.date(),m=b.settings.embedding.model_id,p=b.settings.embedding.provider,t=at-timedelta(seconds=1))
    assert run(b)=='embedding_budget_exhausted'
    assert not b.calls and sql(b,'SELECT count(*) FROM embedding_calls')==0
    assert sql(b,'SELECT sum(reserved_usd) FROM search_usage')==Decimal('0.01')


@pytest.mark.parametrize('state,resume',[('awaiting_index',None),('indexing',None),('retryable_failed','indexing')])
def test_duplicate_claim_and_index_dispatch(indexed,state,resume):
    b=indexed
    sql(b,'UPDATE ingestion_jobs SET state=:s,resume_state=:r WHERE id=:i',s=state,r=resume,i=b.job)
    with Session(b.api.engine) as db: assert b.job in w.due_job_ids(db,b.settings)
    start=threading.Barrier(2)
    def one():
        with Session(b.api.engine) as db:
            start.wait(10)
            return c.claim(db,b.job,b.settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda _:one(),range(2)))
    assert sum(t is not None for t in claims)==1
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==2
    assert not b.opened and not b.calls


@pytest.mark.parametrize('change',['disable','held','ack','intent','owner','cancel','epoch','token','lease','hold'])
@pytest.mark.parametrize('phase',['hash','provider'])
def test_change_at_io_fences_content_but_settles_cost(indexed,monkeypatch,change,phase):
    b=indexed
    def alter(*_):
        if change=='disable': owner_status(b,'disabled')
        elif change=='held': sql(b,"UPDATE users SET status_safety='held' WHERE id=:i",i=b.uid) # adversarial owner setup
        elif change=='ack': sql(b,"UPDATE processing_acknowledgements SET notice_version='wrong'")
        elif change=='intent': sql(b,"UPDATE upload_intents SET declared_sha256=repeat('a',64)")
        elif change=='owner':
            with b.owner.begin() as con:  # labelled owner corruption, no runtime privilege widening
                con.execute(text('ALTER TABLE ingestion_jobs DISABLE TRIGGER USER'))
                con.execute(text('UPDATE ingestion_jobs SET scope_owner_user_id=:u WHERE id=:i'),{'u':b.api.admin_id,'i':b.job})
                con.execute(text('ALTER TABLE ingestion_jobs ENABLE TRIGGER USER'))
        elif change=='cancel': sql(b,'UPDATE ingestion_jobs SET cancel_requested=true,cancel_requested_at=:at WHERE id=:i',at=NOW,i=b.job)
        elif change=='epoch': sql(b,'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',i=b.item)
        elif change=='token': sql(b,'UPDATE ingestion_jobs SET lease_token=:t WHERE id=:i',t=uuid.uuid4(),i=b.job)
        elif change=='lease': sql(b,'UPDATE ingestion_jobs SET lease_until=:at WHERE id=:i',at=NOW,i=b.job)
        else: recovery.hold(b.owner,get_settings(),NOW)
    if phase=='provider': b.action=alter
    else:
        original=b.storage.open
        def opened(key):
            stream=original(key); alter(); return stream
        monkeypatch.setattr(b.storage,'open',opened)
    outcome=run(b)
    expected={'disable':'owner_status_paused','held':'owner_status_paused','ack':'upload_acknowledgement_missing',
        'intent':'upload_acknowledgement_missing','owner':'rights_gate_missing','cancel':'cancelled','epoch':'cancelled',
        'token':'lease_lost','lease':'lease_lost','hold':'recovery_gate_closed'}[change]
    assert outcome==expected
    assert sql(b,'SELECT count(*) FROM index_entries')==0
    assert sql(b,"SELECT count(*) FROM index_generations WHERE state='active'")==0
    assert sql(b,'SELECT count(*) FROM embedding_calls WHERE vector IS NOT NULL')==0
    if phase=='hash': assert not b.calls and sql(b,'SELECT count(*) FROM embedding_calls')==0
    elif change=='hold': assert sql(b,"SELECT count(*) FROM embedding_calls WHERE status='reserved'")==1
    else:
        assert len(b.calls)==1 and sql(b,"SELECT count(*) FROM embedding_calls WHERE status='completed'")==1
        assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0


@pytest.mark.parametrize('reply',['timeout','429','malformed','identity','nonfinite','usage','dimensions'])
def test_transport_outcomes(indexed,reply):
    b=indexed
    def action(request):
        if reply=='timeout': raise httpx.ReadTimeout('synthetic',request=request)
        if reply=='429': return httpx.Response(429)
        if reply=='malformed': return httpx.Response(200,content=b'{')
        return httpx.Response(200,json={'model':'other' if reply=='identity' else 'text-embedding-3-small',
            'provider':'Azure','data':[{'embedding':[float('inf')] if reply=='nonfinite' else [.1,.2]}],
            'usage':{'prompt_tokens':b.settings.embedding.max_request_bytes+1 if reply=='usage' else 3}})
    # JSON forbids Inf in httpx's encoder: malformed transport is exercised separately.
    if reply=='nonfinite':
        b.action=lambda _:httpx.Response(200,content=b'{"model":"text-embedding-3-small","provider":"Azure","data":[{"embedding":[1e400]}]}')
    else: b.action=action
    code=run(b)
    if reply=='dimensions':
        assert code=='ready' and sql(b,'SELECT dimensions FROM index_generations')==2
    else:
        assert code==('provider_rate_limited' if reply=='429' else 'provider_outcome_uncertain' if reply=='timeout' else 'provider_response_invalid')
        assert sql(b,'SELECT count(*) FROM index_entries')==0
        assert sql(b,"SELECT status FROM embedding_calls")==('rejected' if reply=='429' else 'uncertain')
        assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==(0 if reply=='429' else sql(b,'SELECT reserved_usd FROM embedding_calls'))


@pytest.mark.parametrize('phase',['index_claim','index_proof','reservation','pre_send','settlement','entry','publish'])
@pytest.mark.parametrize('landed',[False,True])
def test_commit_uncertain_never_continues_or_fails(indexed,monkeypatch,phase,landed):
    b=indexed; observed=[]
    original=runtime.commit
    def record(db,outcome):
        try: return original(db,outcome)
        finally:
            if db.info.get('document_checkpoint')==phase: observed.append(outcome)
    monkeypatch.setattr(runtime,'commit',record)
    class Fault(Session):
        fired=False
        def commit(self):
            if self.info.get('document_checkpoint')==phase and not self.fired:
                self.fired=True
                super().commit() if landed else super().rollback()
                raise RuntimeError('synthetic lost commit reply')
            return super().commit()
    monkeypatch.setattr(w,'get_sessionmaker',lambda:lambda:Fault(b.api.engine,expire_on_commit=False))
    assert run(b)=='worker_commit_uncertain'
    assert observed and observed[-1].commit_started and not observed[-1].committed
    assert sql(b,"SELECT count(*) FROM ingestion_jobs WHERE state IN ('terminal_failed','retryable_failed')")==0
    if phase in ('index_claim','index_proof','reservation','pre_send'): assert not b.calls
    else: assert len(b.calls)==(b.expected if phase=='publish' else 1)
    if phase=='publish' and landed: assert sql(b,"SELECT state FROM ingestion_jobs WHERE id=:i",i=b.job)=='ready'
    else: assert sql(b,"SELECT count(*) FROM index_generations WHERE state='active'")==0


@pytest.mark.parametrize('phase',['reservation','pre_send','settlement','entry','publish'])
@pytest.mark.parametrize('fault',['rollback','close','both'])
def test_known_commit_release_error_stops(indexed,monkeypatch,phase,fault):
    b=indexed; observed=[]; invalidated=[]
    original=runtime.commit
    def record(db,outcome):
        result=original(db,outcome)
        if db.info.get('document_checkpoint')==phase: observed.append(outcome)
        return result
    monkeypatch.setattr(runtime,'commit',record)
    class Fault(Session):
        def rollback(self):
            super().rollback()
            if observed and fault in ('rollback','both'): raise RuntimeError('synthetic release')
        def close(self):
            super().close()
            if observed and fault in ('close','both'): raise RuntimeError('synthetic release')
        def invalidate(self):
            invalidated.append(True); super().invalidate()
    monkeypatch.setattr(w,'get_sessionmaker',lambda:lambda:Fault(b.api.engine,expire_on_commit=False))
    assert run(b)=='worker_release_unavailable'
    assert observed[-1].committed and invalidated
    assert len(b.calls)==(0 if phase in ('reservation','pre_send') else b.expected if phase=='publish' else 1)
    assert sql(b,"SELECT count(*) FROM ingestion_jobs WHERE state IN ('terminal_failed','retryable_failed')")==0


@pytest.mark.parametrize('which',['reservation','pre_send','publish'])
@pytest.mark.parametrize('expiry',['equal','after'])
def test_clock_after_final_sql(indexed,monkeypatch,which,expiry):
    b=indexed; armed=False; deadline=None
    # Isolate the job deadline from the independently shorter reservation lease.
    b.settings.embedding.reservation_lease_seconds=300
    original=auth_safety.require_final
    def gate(db,**kw):
        nonlocal armed,deadline
        original(db,**kw)
        if db.info.get('document_checkpoint')==which and kw.get('writer'):
            deadline=sql(b,'SELECT lease_until FROM ingestion_jobs WHERE id=:i',i=b.job)
            armed=True
    def late(conn,cur,stmt,params,ctx,many):
        if armed and 'ras_auth_status_live' in stmt:
            monkeypatch.setattr(w,'utcnow',lambda:deadline+timedelta(microseconds=expiry=='after'))
    monkeypatch.setattr(auth_safety,'require_final',gate)
    event.listen(get_engine(),'after_cursor_execute',late)
    try: assert run(b)=='lease_lost'
    finally: event.remove(get_engine(),'after_cursor_execute',late)
    assert armed and sql(b,"SELECT count(*) FROM index_generations WHERE state='active'")==0
    if which!='publish': assert not b.calls


@pytest.mark.parametrize('order',['renew_first','reap_first'])
def test_heartbeat_reaper_and_late_settlement_once(indexed,monkeypatch,order):
    b=indexed; db,token,a,v=prepare(b)
    old=sql(b,'SELECT lease_until FROM embedding_calls WHERE id=:i',i=a.call.id)
    if order=='renew_first':
        monkeypatch.setattr(w,'utcnow',lambda:old-timedelta(seconds=1))
        assert money.renew(b.api.engine,a.call,b.settings)
        assert money.reap(db,b.settings)==0
    deadline=sql(b,'SELECT lease_until FROM embedding_calls WHERE id=:i',i=a.call.id)
    monkeypatch.setattr(w,'utcnow',lambda:deadline)
    assert not money.renew(b.api.engine,a.call,b.settings)
    assert money.reap(db,b.settings)==1
    assert not money.renew(b.api.engine,a.call,b.settings)
    before=sql(b,'SELECT reserved_usd FROM embedding_budget_days')
    assert before>0
    assert money.settle(db,a.call,b.settings,result=result(b))=='completed'
    assert money.settle(db,a.call,b.settings,result=result(b))=='completed'
    assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0
    assert sql(b,'SELECT committed_usd FROM embedding_budget_days')==ix._amount(3,b.settings)
    assert sql(b,'SELECT vector IS NULL FROM embedding_calls')


def test_old_nonce_cannot_renew_or_settle_re_reserved_row(indexed):
    b=indexed; db,token,a,v=prepare(b)
    assert money.settle(db,a.call,b.settings,error=EmbeddingError('429',True))=='rejected'
    a2=c.step(db,b.job,token,b.settings,a.source,v)
    assert a2.send and a2.call.id==a.call.id and a2.call.reservation_token!=a.call.reservation_token
    assert not money.renew(b.api.engine,a.call,b.settings)
    with pytest.raises(runtime.Halt,match='reservation_lost'): money.settle(db,a.call,b.settings,result=result(b))
    assert sql(b,"SELECT status FROM embedding_calls")=='reserved'
    assert money.settle(db,a2.call,b.settings,result=result(b))=='completed'
    assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0


def test_original_day_settlement_after_midnight(indexed,monkeypatch):
    b=indexed; db,token,a,v=prepare(b)
    monkeypatch.setattr(w,'utcnow',lambda:NOW+timedelta(days=1))
    assert money.reap(db,b.settings)==1
    assert money.settle(db,a.call,b.settings,result=result(b))=='completed'
    assert sql(b,'SELECT day FROM embedding_budget_days')==NOW.date()
    assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==0


@pytest.mark.parametrize('change',['size','same_size','lying_head'])
def test_full_hash_blocks_original_overwrite(indexed,monkeypatch,change):
    b=indexed
    if change=='lying_head': monkeypatch.setattr(b.storage,'fingerprint',lambda _:('unchanged',))
    raw=BODY+b' ' if change=='size' else BODY.replace(b'measured',b'altered!')
    b.storage.put(b.key,io.BytesIO(raw),'text/markdown')
    assert run(b)=='checksum_mismatch' and not b.calls
    assert sql(b,'SELECT count(*) FROM embedding_calls')==0


def test_completed_reuse_still_hashes_after_final_entry(indexed,monkeypatch):
    b=indexed; db,token,a,v=prepare(b)
    money.settle(db,a.call,b.settings,result=result(b))
    c.store_result(db,b.job,token,b.settings,a,result(b),v)
    while True:
        next_admission=c.step(db,b.job,token,b.settings,a.source,v)
        if next_admission is None: break
        if next_admission.send:
            money.settle(db,next_admission.call,b.settings,result=result(b))
            c.store_result(db,b.job,token,b.settings,next_admission,result(b),v)
    sql(b,'DELETE FROM index_entries')
    sql(b,'UPDATE ingestion_jobs SET lease_until=:at WHERE id=:i',at=NOW,i=b.job)
    original=ix._insert_entry
    def changed(*args):
        original(*args)
        # Same size, HEAD lies. Only the mandatory post-entry full hash detects it.
        b.storage.put(b.key,io.BytesIO(BODY.replace(b'measured',b'altered!')),'text/markdown')
    # Mutating storage from the hook simulates an independent object writer; no
    # production IO is added to the transaction.
    monkeypatch.setattr(ix,'_insert_entry',changed)
    monkeypatch.setattr(b.storage,'fingerprint',lambda _:('unchanged',))
    assert run(b)=='checksum_mismatch' and not b.calls
    assert sql(b,"SELECT count(*) FROM index_generations WHERE state='active'")==0
    assert sql(b,'SELECT count(*) FROM embedding_calls')==b.expected


@pytest.mark.parametrize('guard',['pending','hold','missing'])
def test_real_gate_prevents_entry_and_maintenance(indexed,monkeypatch,guard):
    b=indexed
    if guard=='pending': monkeypatch.setattr(auth_safety,'cutover',lambda db:'auth_safety_pending')
    elif guard=='hold': recovery.hold(b.owner,get_settings(),NOW)
    else: b.path.unlink()
    assert run(b)=='recovery_gate_closed' and not b.calls and not b.opened
    with Session(b.api.engine) as db:
        with pytest.raises(runtime.Halt,match='recovery_gate_closed'): money.reap(db,b.settings)
    assert b.browser.get('/healthz').status_code==200 and b.browser.get('/readyz').status_code==503


@pytest.mark.parametrize('role',['owner','recovery','missing_grant'])
def test_direct_index_entry_requires_actual_runtime(indexed,monkeypatch,role):
    import os
    from app.db import make_engine
    b=indexed; engine=b.owner
    if role=='recovery': engine=make_engine(b.owner.url.set(username='c1_recovery',password=os.environ['RAS_C1_RECOVERY_PASSWORD']))
    elif role=='missing_grant':
        sql(b,'REVOKE UPDATE ON embedding_calls FROM c1_runtime'); engine=b.api.engine
    before=sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)
    with Session(engine) as db:
        try:
            c.claim(db,b.job,b.settings)
            code='claimed'
        except runtime.Halt as exc:
            code=exc.code
    assert code=='worker_runtime_required'
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==before
    assert not b.calls and not b.opened
    if role=='recovery': engine.dispose()


@pytest.mark.parametrize('change',['disable','held','head'])
def test_owner_paused_before_claim(indexed,change):
    b=indexed
    if change=='disable': owner_status(b,'disabled')
    elif change=='held': sql(b,"UPDATE users SET status_safety='held' WHERE id=:i",i=b.uid)
    else:
        with b.owner.begin() as con:  # labelled corruption of a protected projection
            con.execute(text('ALTER TABLE auth_safety_heads DISABLE TRIGGER USER'))
            con.execute(text('DELETE FROM auth_safety_heads WHERE user_id=:i'),{'i':b.uid})
            con.execute(text('ALTER TABLE auth_safety_heads ENABLE TRIGGER USER'))
    before=sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)
    expected = 'recovery_gate_closed' if change == 'head' else 'owner_status_paused'
    assert run(b)==expected and not b.calls and not b.opened
    assert sql(b,'SELECT attempt_count FROM ingestion_jobs WHERE id=:i',i=b.job)==before


@pytest.mark.parametrize('change',['chunk_hash','locator','section_file','pipeline','coverage'])
def test_corrupt_derivatives_never_leave_for_provider(indexed,change):
    b=indexed
    statements={
        'chunk_hash':"UPDATE chunks SET chunk_hash=repeat('0',64) WHERE item_id=:i",
        'locator':"UPDATE chunks SET start_offset=start_offset+1 WHERE item_id=:i",
        'section_file':"UPDATE sections SET text=text||' forged' WHERE item_id=:i AND kind='body'",
        'pipeline':"UPDATE chunks SET pipeline_version='markdown-legacy' WHERE item_id=:i",
        'coverage':"UPDATE paper_versions SET coverage='none' WHERE item_id=:i"}
    sql(b,statements[change],i=b.item)
    assert run(b) in ('index_chunk_invalid','index_locator_invalid','index_body_missing','index_rights_or_coverage_invalid')
    assert not b.calls and sql(b,'SELECT count(*) FROM embedding_calls')==0


def test_heartbeat_clock_after_final_wait_cannot_resurrect(indexed,monkeypatch):
    b=indexed; db,token,a,v=prepare(b)
    old=a.call.lease_until
    original=auth_safety.require_final
    def gate(db,**kw):
        original(db,**kw)
        if db.info.get('document_checkpoint')=='heartbeat':
            monkeypatch.setattr(w,'utcnow',lambda:old)
    monkeypatch.setattr(auth_safety,'require_final',gate)
    with pytest.raises(runtime.Halt,match='reservation_lost'): money.renew(b.api.engine,a.call,b.settings)
    assert sql(b,'SELECT lease_until FROM embedding_calls WHERE id=:i',i=a.call.id)==old
    assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==a.call.reserved_usd


@pytest.mark.parametrize('deadline', ['expired', 'absent'])
def test_historic_reservation_expires_without_adoption(indexed, deadline):
    b=indexed; db,token,a,v=prepare(b)
    # Labelled historical ledger fixture: migration intentionally retains NULL
    # attempt bindings. Expiry must free the slot without inventing an attempt.
    sql(b, '''UPDATE embedding_calls SET reservation_token=NULL,reservation_job_id=NULL,
        reservation_job_token=NULL,lease_until=:t WHERE id=:i''',
        t=NOW-timedelta(seconds=1) if deadline=='expired' else None, i=a.call.id)
    assert money.reap(db,b.settings)==1
    assert sql(b,"SELECT status FROM embedding_calls WHERE id=:i",i=a.call.id)=='uncertain'
    assert sql(b,'SELECT reserved_usd FROM embedding_budget_days')==a.call.reserved_usd
    assert sql(b,'SELECT committed_usd FROM embedding_budget_days')==0
    assert sql(b,'SELECT reservation_token IS NULL FROM embedding_calls WHERE id=:i',i=a.call.id)
    with pytest.raises(runtime.Halt,match='reservation_lost'):
        money.settle(db,a.call,b.settings,result=result(b))
    assert money.renew(b.api.engine,a.call,b.settings) is False
    assert not b.calls and sql(b,'SELECT count(*) FROM index_entries')==0


@pytest.mark.parametrize('phase',['heartbeat','settlement','reaper'])
@pytest.mark.parametrize('landed',[False,True])
def test_financial_commit_uncertain_keeps_attempt_fact(indexed,monkeypatch,phase,landed):
    b=indexed; db,token,a,v=prepare(b); seen=[]
    original=runtime.commit
    def record(db,outcome):
        try: return original(db,outcome)
        finally: seen.append(outcome)
    monkeypatch.setattr(runtime,'commit',record)
    class Fault(Session):
        def commit(self):
            if self.info.get('document_checkpoint')==phase:
                super().commit() if landed else super().rollback()
                raise RuntimeError('synthetic financial commit loss')
            return super().commit()
    if phase=='heartbeat':
        monkeypatch.setattr(money,'Session',lambda *a,**kw:Fault(b.api.engine,expire_on_commit=False))
        operation=lambda:money.renew(b.api.engine,a.call,b.settings)
    elif phase=='reaper':
        monkeypatch.setattr(w,'utcnow',lambda:a.call.lease_until)
        operation=lambda:money.reap(Fault(b.api.engine,expire_on_commit=False),b.settings)
    else: operation=lambda:money.settle(Fault(b.api.engine,expire_on_commit=False),a.call,b.settings,result=result(b))
    with pytest.raises(runtime.Halt,match='worker_commit_uncertain'): operation()
    assert seen[-1].commit_started and not seen[-1].committed
    assert not b.calls and sql(b,'SELECT count(*) FROM index_entries')==0
    assert sql(b,'SELECT vector IS NULL FROM embedding_calls')
    committed=sql(b,'SELECT committed_usd FROM embedding_budget_days')
    assert committed==(ix._amount(3,b.settings) if phase=='settlement' and landed else 0)
