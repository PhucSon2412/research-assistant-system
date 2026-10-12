"""Real restricted runtime/PG/journal and generation adapter with fake transport."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import rag_runtime as rag, search_accounting as money, search_boundary as boundary
from app.api.errors import ApiError
from app.ingest.checkpoints import Record, frozen
from app.models import SearchUsage
from tests.test_c2d6_search_runtime import ready  # noqa: F401
from tests.test_c2d5d2b_document_runtime import indexed  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work, sql, owner_status  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_uploads import upload_api  # noqa: F401


def completion(b, payload):
    evidence = json.loads(payload['messages'][1]['content'])['evidence']
    assert evidence and all('arXiv:' not in r['text'] for r in evidence)
    first = evidence[0]
    return {'object': 'chat.completion', 'model': b.settings.generation.model_id,
        'openrouter_metadata': {'requested': b.settings.generation.model_id, 'endpoints': {'available': [
            {'selected': True, 'provider': 'SyntheticProvider', 'model': b.settings.generation.model_id}]}},
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'claims': [
            {'text': 'A measured result.', 'citations': [{'chunk_id': first['chunk_id'], 'quote': 'measured'}]}]})}}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 10}}


@pytest.fixture
def generated(ready):
    b = ready
    b.settings.generation.pricing_verified = True  # in-memory copy + MockTransport ONLY
    b.gen_action = None
    def action(request, payload):
        if 'messages' not in payload:
            return None
        assert b.http_engine.pool.checkedout() == 0
        assert not payload['stream']
        if b.gen_action:
            changed = b.gen_action(request, payload)
            if changed is not None:
                return changed
        return httpx.Response(200, json=completion(b, payload))
    b.action = action
    return b


def ask(b, **body):
    return b.browser.post('/rag/ask', {'scope': 'all', 'question': 'measured', 'mode': 'keyword'} | body)


def proof(b):
    with Session(b.api.engine) as db:
        row=db.query(SearchUsage).filter_by(task_type='generation').order_by(SearchUsage.created_at).first()
        return frozen(row) if row is not None else None


@pytest.mark.parametrize('mode', ['keyword', 'hybrid'])
@pytest.mark.parametrize('scope', ['item', 'collection', 'all'])
def test_runtime_scopes_adapter_citation_and_single_counters(generated, mode, scope):
    b = generated
    body = {'scope': None, 'item_id': str(b.item)} if scope == 'item' else {}
    if scope == 'collection':
        c = b.browser.post('/collections', {'name': 'RAG runtime'}).json()['collection']['id']
        assert b.browser.post('/collections/'+c+'/items', {'item_id': str(b.item)}).status_code == 201
        body = {'scope': None, 'collection_id': c}
    response = ask(b, mode=mode, **body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['status'] == 'answered' and data['scope']['kind'] == scope
    cite = data['claims'][0]['citations'][0]
    opened = b.browser.get(cite['url'].replace('/sources/', '/search/chunks/'))
    assert opened.status_code == 200
    for key in ('item_id','version_id','source_file_id','locator_kind','start_line','end_line','original_url'):
        assert cite[key] == opened.json()[key]
    assert cite['quote'] in opened.json()['text']
    assert len(b.calls) == (2 if mode == 'hybrid' else 1)
    p = proof(b)
    assert p.reservation_token and p.session_id and p.operation_id and p.status == 'completed'
    assert sql(b, 'SELECT request_count FROM rag_request_days') == 1
    assert sql(b, 'SELECT request_count FROM search_request_days') == 1
    assert b.http_engine.pool.checkedout() == 0


@pytest.mark.parametrize('phase', ['counter','reservation','reaper','settlement'])
@pytest.mark.parametrize('landed', [False, True])
def test_commit_uncertain_never_answers_or_resends(generated, monkeypatch, phase, landed):
    b = generated; original = Session.commit
    def fail(db):
        selected = db.info.get('rag_phase') == phase or (phase in ('reaper','settlement') and db.info.get('search_phase') == phase)
        if selected:
            if landed: original(db)
            raise RuntimeError('private fault')
        return original(db)
    monkeypatch.setattr(Session, 'commit', fail)
    response = ask(b)
    assert response.status_code == 503 and response.json() == {'error':'rag_commit_uncertain'}
    assert len(b.calls) == (1 if phase == 'settlement' else 0)
    assert b.http_engine.pool.checkedout() == 0


@pytest.mark.parametrize('phase', ['entry','scope','counter','pack','reservation','presend','settlement','response'])
@pytest.mark.parametrize('method', ['rollback','close'])
def test_release_fault_invalidates_and_withholds_all_content(generated, monkeypatch, phase, method):
    b=generated; original=getattr(Session,method); invalidated=[]; invalidate=Session.invalidate
    def fail(db):
        selected=db.info.get('rag_phase')==phase or (phase=='settlement' and db.info.get('search_phase')==phase)
        if selected:
            original(db)
            raise RuntimeError('private teardown')
        return original(db)
    def observed(db):
        invalidated.append(True);return invalidate(db)
    monkeypatch.setattr(Session,method,fail);monkeypatch.setattr(Session,'invalidate',observed)
    response=ask(b)
    assert response.status_code==503 and response.json()=={'error':'rag_release_unavailable'}
    assert invalidated and b.http_engine.pool.checkedout()==0
    assert len(b.calls)==(1 if phase in ('settlement','response') else 0)


@pytest.mark.parametrize('change', ['epoch','generation','logout','disable','role','membership','reset','session_generation','head'])
@pytest.mark.parametrize('stage', ['before_retrieval','after_admission','provider','response'])
def test_exact_scope_and_caller_at_each_external_boundary(generated, monkeypatch, change, stage):
    b=generated; body={}; collection=None
    if change=='membership':
        collection=b.browser.post('/collections',{'name':'Race'}).json()['collection']['id']
        assert b.browser.post('/collections/'+collection+'/items',{'item_id':str(b.item)}).status_code==201
        body={'scope':None,'collection_id':collection}
    def mutate():
        if change=='logout': assert b.browser.post('/auth/logout',{}).status_code==204
        elif change=='reset':
            browser=b.api.browser()
            assert browser.post('/auth/password-reset/request',{'email':'worker@example.test'}).status_code==202
            token=b.api.last_link_token('password_reset','worker@example.test')
            assert browser.post('/auth/password-reset/confirm',{'token':token,'new_password':'brand-new-Passw0rd'}).status_code==200
        elif change in ('session_generation','head'):
            # Labelled owner corruption fixture; production immutability triggers
            # reject these writes. Test the independent final caller predicates.
            with b.owner.begin() as conn:
                conn.execute(text("SET LOCAL session_replication_role='replica'"))
                table='sessions' if change=='session_generation' else 'auth_safety_heads'
                conn.execute(text(f'UPDATE {table} SET credential_generation=:g WHERE user_id=:u'),
                    {'g':uuid.uuid4(),'u':b.uid})
        elif change=='disable': owner_status(b,'disabled')
        elif change=='role': sql(b,"UPDATE users SET role='admin' WHERE id=:u",u=b.uid)
        elif change=='membership': sql(b,'DELETE FROM collection_items WHERE collection_id=:c',c=uuid.UUID(collection))
        elif change=='epoch': sql(b,'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',i=b.item)
        else: sql(b,"UPDATE index_generations SET state='superseded' WHERE item_id=:i",i=b.item)
    if stage=='provider':
        entered,released=Event(),Event()
        def blocked(*_):
            entered.set();assert released.wait(20)
        b.gen_action=blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(ask,b,**body)
            try:
                assert entered.wait(20)
                assert b.http_engine.pool.checkedout()==0
                mutate()  # Commits while HTTP remains blocked, without a held resource lock.
            finally: released.set()
            response=future.result(20)
    else:
        original=rag.checkpoint
        def changed(engine,phase,operation):
            if stage=='response' and phase=='response': mutate()
            result=original(engine,phase,operation)
            if stage=='after_admission' and phase=='reservation': mutate()
            if stage=='before_retrieval' and phase=='counter': mutate()
            return result
        monkeypatch.setattr(rag,'checkpoint',changed)
        response=ask(b,**body)
    assert response.status_code==(401 if change in ('logout','disable','reset','session_generation','head') else 409),response.text
    assert 'measured' not in response.text and 'claims' not in response.text
    assert len(b.calls)==(0 if stage in ('after_admission','before_retrieval') else 1)
    if stage=='before_retrieval':
        assert proof(b) is None
    else:
        assert proof(b).status==('reserved' if stage=='after_admission' else 'completed')


@pytest.mark.parametrize('phase,question,pricing', [('response','the and',True),('response','absenttoken',True),
    ('response','measured',True),('unavailable','measured',False)])
@pytest.mark.parametrize('ttl', ['idle','absolute'])
def test_final_clock_after_last_predicate_sql_for_every_response(generated, phase, question, pricing, ttl):
    b=generated;b.settings.generation.pricing_verified=pricing
    delta=b.settings.auth.session_idle if ttl=='idle' else b.settings.auth.session_absolute
    count=[]
    def mark(db,transaction,conn): conn.info['rag_test_phase']=db.info.get('rag_phase')
    def advance(conn,cursor,statement,params,context,executemany):
        if conn.info.get('rag_test_phase')==phase and 'ras_auth_session_live' in statement:
            count.append(True)
            if len(count)==(3 if phase=='response' else 2): b.api.clock.now+=delta
    event.listen(Session,'after_begin',mark);event.listen(b.http_engine,'after_cursor_execute',advance)
    try:
        response=ask(b,question=question)
        assert response.status_code==401 and response.json()['error']=='session_expired',response.text
    finally:
        event.remove(Session,'after_begin',mark);event.remove(b.http_engine,'after_cursor_execute',advance)


@pytest.mark.parametrize('field', money.FIELDS)
def test_generation_nonce_cannot_adopt_another_financial_tuple(generated, field):
    b=generated; assert ask(b).status_code==200
    p=proof(b);values=dict(p.values);v=values[field]
    values[field]=(v+timedelta(days=1) if field=='day' else v+Decimal('.01') if field=='reserved_usd'
        else 'embed_query' if field=='task_type' else 'forged' if field in ('model_id','provider') else uuid.uuid4())
    forged=Record(values)
    assert money.renew(b.api.engine,forged,b.settings,clock=lambda:b.api.clock.now) is False
    with pytest.raises(ApiError,match='search_reservation_lost'):
        money.settle(b.api.engine,forged,b.settings,tokens=1,output_tokens=1,clock=lambda:b.api.clock.now)
    assert proof(b).input_tokens==20 and proof(b).output_tokens==10


@pytest.mark.parametrize('malformed', ['nested','bool','huge','model','provider','choices','timeout','rejected','usage'])
def test_real_adapter_malformed_identity_and_usage_keep_ceiling(generated, malformed):
    b=generated
    def changed(request,payload):
        data=completion(b,payload)
        if malformed=='timeout': raise httpx.ReadTimeout('synthetic',request=request)
        if malformed=='rejected': return httpx.Response(429)
        if malformed=='nested': data['openrouter_metadata']['endpoints']=[]
        elif malformed=='bool': data['usage']['prompt_tokens']=True
        elif malformed=='huge': data['usage']['completion_tokens']=10**200
        elif malformed=='model': data['model']='wrong'
        elif malformed=='provider': data['openrouter_metadata']['endpoints']['available']=[]
        elif malformed=='choices': data['choices']=[None]
        elif malformed=='usage': data['usage']=None
        return httpx.Response(200,json=data)
    b.gen_action=changed
    response=ask(b)
    assert response.status_code==503 and len(b.calls)==1,response.text
    p=proof(b)
    assert p.status==('rejected' if malformed=='rejected' else 'uncertain')
    assert p.cost_usd==(Decimal(0) if malformed=='rejected' else None)
    assert p.reserved_usd>0 and b.http_engine.pool.checkedout()==0


def test_lease_old_day_late_duplicate_settlement_and_legacy_refusal(generated):
    b=generated;b.gen_action=lambda *_:httpx.Response(500)
    assert ask(b).status_code==503
    p=proof(b); day=p.day
    b.api.clock.now+=timedelta(days=1)
    assert money.renew(b.api.engine,p,b.settings,clock=lambda:b.api.clock.now) is False
    for _ in range(2):
        cost=money.settle(b.api.engine,p,b.settings,tokens=20,output_tokens=10,
            response_model_id=p.model_id,response_provider='SyntheticProvider',clock=lambda:b.api.clock.now)
        assert cost['status']=='usage_price_estimate'
    assert proof(b).day==day and proof(b).input_tokens==20
    values=dict(p.values);values['reservation_token']=None
    with pytest.raises(ApiError,match='search_reservation_lost'):
        money.settle(b.api.engine,Record(values),b.settings,tokens=0,output_tokens=0,clock=lambda:b.api.clock.now)


def test_heartbeat_fault_settles_finance_but_never_answer(generated, monkeypatch):
    b=generated
    @contextmanager
    def failed(*args,**kwargs):
        flag=Event();flag.set();yield flag
    monkeypatch.setattr(money,'heartbeat',failed)
    response=ask(b)
    assert response.json()=={'error':'rag_heartbeat_unavailable'} and response.status_code==503
    assert proof(b).status=='completed' and len(b.calls)==1
    assert not any(t.name=='search-reservation-heartbeat' for t in threading.enumerate())


def test_concurrent_generation_has_one_slot_and_no_second_dispatch(generated):
    b=generated;b.settings.generation.max_concurrent_requests=1
    entered,released=Event(),Event()
    def blocked(*_): entered.set();assert released.wait(20)
    b.gen_action=blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(ask,b)
        try:
            assert entered.wait(20)
            response=ask(b)
            assert response.status_code==429 and response.json()['error']=='rag_generation_concurrency_limit'
            assert len(b.calls)==1
        finally: released.set()
        assert future.result(20).json()['status']=='answered'
    assert sql(b,"SELECT count(*) FROM search_usage WHERE task_type='generation'")==1


@pytest.mark.parametrize('task', ['embed_query','rerank','generation','document'])
def test_generation_shares_application_budget_and_keeps_uncertain_ceilings(generated, task):
    b=generated;b.settings.budget.app_daily_usd=.01
    if task=='document':
        sql(b,'UPDATE embedding_budget_days SET committed_usd=0.01,reserved_usd=0 WHERE day=:d',d=b.api.clock.now.date())
    else:
        sql(b,"""INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,reserved_usd,cost_status)
            VALUES (:u,:d,:t,'synthetic','openrouter','uncertain',0.01,'unknown_reserved_ceiling')""",
            u=b.uid,d=b.api.clock.now.date(),t=task)
    response=ask(b)
    assert response.status_code==429 and response.json()['error']=='search_app_budget_exhausted',response.text
    assert not b.calls


@pytest.mark.parametrize('limit,code', [('generation_daily_usd','rag_generation_budget_exhausted'),
    ('user_generation_daily_usd','rag_user_budget_exhausted')])
def test_generation_bucket_and_user_quota(generated, limit, code):
    b=generated;setattr(b.settings.budget,limit,.00000001)
    response=ask(b)
    assert response.status_code==429 and response.json()['error']==code
    assert not b.calls


def test_warning_is_counted_and_trusted_usage_uses_both_prices(generated):
    b=generated
    p=b.settings.generation
    ceiling=rag.rag.money(p.max_input_tokens_estimate,p.max_input_usd_per_million_tokens)+rag.rag.money(p.max_output_tokens,p.max_output_usd_per_million_tokens)
    b.settings.budget.generation_daily_usd=float(ceiling)
    response=ask(b)
    assert response.status_code==200,response.text
    cost=response.json()['cost']
    assert 'generation_bucket_near_limit' in cost['warnings']
    assert Decimal(cost['estimated_usd'])==rag.rag.money(20,p.max_input_usd_per_million_tokens)+rag.rag.money(10,p.max_output_usd_per_million_tokens)


def test_expiry_releases_slot_without_resurrection_and_holds_money(generated):
    b=generated;b.gen_action=lambda *_:httpx.Response(500)
    assert ask(b).status_code==503
    p=proof(b)
    sql(b,"UPDATE search_usage SET status='reserved',lease_until=:t WHERE id=:i",i=p.id,t=b.api.clock.now-timedelta(seconds=1))
    money.reap(b.api.engine,b.settings,clock=lambda:b.api.clock.now)
    assert proof(b).status=='uncertain' and proof(b).reserved_usd==p.reserved_usd
    assert money.renew(b.api.engine,p,b.settings,clock=lambda:b.api.clock.now) is False
    assert sql(b,'SELECT cost_usd IS NULL AND lease_until IS NULL FROM search_usage WHERE id=:i',i=p.id)


def test_cancellation_preserves_ceiling_and_joins_thread(generated, monkeypatch):
    b=generated
    from app.authz import Principal
    sid=sql(b,'SELECT id FROM sessions WHERE user_id=:u ORDER BY created_at DESC LIMIT 1',u=b.uid)
    principal=Principal(b.uid,'user',sid,'worker@example.test',None)
    class Cancelled(BaseException): pass
    def cancel(*_): raise Cancelled()
    monkeypatch.setattr(rag.rag,'generate',cancel)
    with pytest.raises(Cancelled):
        rag.answer(b.api.engine,principal,b.settings,None,'measured','keyword',all_items=True,
            clock=lambda:b.api.clock.now,csrf=b.browser.client.cookies.get('ras_csrf'))
    assert proof(b).status=='reserved' and proof(b).cost_usd is None
    assert not any(t.name=='search-reservation-heartbeat' for t in threading.enumerate())
    assert b.http_engine.pool.checkedout()==0


def test_0048_preserves_historical_rows_and_refuses_lossy_downgrade(generated):
    b=generated
    from alembic import command
    from tests.conftest import alembic_config
    from sqlalchemy.exc import IntegrityError
    from tests.conftest import HEAD_REVISION
    cfg=alembic_config(b.owner.url)
    # C3 (0049) refuses any downgrade of a registered installation before 0048 is reached, so the
    # historical all-NULL row is written at head by the owner (labelled pre-0048 writer shape).
    with pytest.raises(Exception,match='auth safety history prevents downgrade'):
        command.downgrade(cfg,'0047')
    old=sql(b,"""INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,reserved_usd,cost_status)
        VALUES (:u,:d,'generation','historical','openrouter','uncertain',0.01,'unknown') RETURNING id""",
        u=b.uid,d=b.api.clock.now.date())
    assert sql(b,'SELECT reservation_token IS NULL AND operation_id IS NULL AND session_id IS NULL FROM search_usage WHERE id=:i',i=old)
    assert ask(b).status_code==200
    # The first row is historical; select the new nonce for independent DB checks.
    new=sql(b,"SELECT id FROM search_usage WHERE reservation_token IS NOT NULL AND task_type='generation'")
    with pytest.raises(IntegrityError), b.owner.begin() as conn:
        conn.execute(text('UPDATE search_usage SET session_id=NULL WHERE id=:i'),{'i':new})
    with pytest.raises(Exception,match='auth safety history prevents downgrade'):
        command.downgrade(cfg,'0047')
    assert sql(b,'SELECT version_num FROM alembic_version')==HEAD_REVISION


def test_evaluation_captures_actual_runtime_retrieval_and_pack(generated):
    from app.evaluation.runner import Capture, instrumented
    b=generated;capture=Capture()
    with instrumented(capture):
        assert b.browser.post('/search',{'query':'measured','mode':'keyword'}).status_code==200
        assert capture.rag_hits==[]
        response=ask(b)
    assert response.status_code==200 and response.json()['status']=='insufficient_evidence',response.text
    assert len(capture.rag_hits)==len(capture.payloads)==1
    assert capture.payloads[0]['user']['evidence']
    ids={h['chunk_id'] for h in capture.rag_hits[0]}
    assert {h['chunk_id'] for h in capture.payloads[0]['user']['evidence']}<=ids
    assert not capture.blocked_hosts and b.calls==[]


@pytest.mark.parametrize('pricing', [False, True])
def test_journal_wait_expiry_blocks_empty_and_unavailable(generated, monkeypatch, pricing):
    import time
    from app import auth_safety, recovery
    b=generated;b.settings.generation.pricing_verified=pricing
    entered,proceed=Event(),Event();original=auth_safety.require_final
    phase='response' if pricing else 'unavailable'
    def paused(db,**kw):
        if db.info.get('rag_phase')==phase:
            entered.set();assert proceed.wait(20)
        return original(db,**kw)
    monkeypatch.setattr(auth_safety,'require_final',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(ask,b,question='the and')
        assert entered.wait(20)
        with b.owner.connect() as conn:
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':recovery.JOURNAL_LOCK})
            proceed.set();deadline=time.monotonic()+5;waiting=0
            while time.monotonic()<deadline:
                waiting=sql(b,"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'")
                if waiting: break
                time.sleep(.01)
            assert waiting,'RAG never waited on the real journal lock'
            b.api.clock.now+=b.settings.auth.session_idle
            conn.rollback()
        response=future.result(20)
    assert response.status_code==401 and response.json()['error']=='session_expired'
    assert not b.calls and b.http_engine.pool.checkedout()==0


def test_actual_heartbeat_thread_failure_is_not_degraded_success(generated, monkeypatch):
    b=generated;b.settings.generation.reservation_lease_seconds=3
    attempted=Event()
    def fail(*args,**kwargs):
        attempted.set();raise ApiError(503,'synthetic_heartbeat_failure')
    monkeypatch.setattr(money,'renew',fail)
    b.gen_action=lambda *_: (attempted.wait(10) and None)
    response=ask(b)
    assert attempted.is_set() and response.status_code==503
    assert response.json()=={'error':'rag_heartbeat_unavailable'} and proof(b).status=='completed'
    assert not any(t.name=='search-reservation-heartbeat' for t in threading.enumerate())


def test_response_limit_keeps_uncertain_ceiling(generated):
    b=generated
    b.gen_action=lambda *_:httpx.Response(200,content=b'x'*(b.settings.generation.max_response_bytes+1))
    response=ask(b)
    assert response.status_code==503 and response.json()=={'error':'rag_provider_response_invalid'}
    assert proof(b).status=='uncertain' and len(b.calls)==1
