"""Real runtime login, PostgreSQL/MinIO/journal; fake HTTP at transport only."""
import json
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app import auth_safety, search as legacy, search_runtime as search, search_accounting as money, search_boundary as boundary
from app import recovery
from app.api.errors import ApiError
from app.authz import Principal
from app.config import get_settings
from app.db import get_engine
from app.ingest import runtime
from app.ingest.checkpoints import Record, frozen
from app.main import app
from app.models import SearchUsage
from tests.test_c2d5d2b_document_runtime import indexed, run  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work, sql, owner_status  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_uploads import upload_api  # noqa: F401


@pytest.fixture
def ready(indexed, monkeypatch):
    b = indexed
    assert run(b) == 'ready'
    app.dependency_overrides[get_settings] = lambda: b.settings
    b.calls.clear()
    b.action = None
    b.settings.rerank.pricing_verified = True  # in-memory fixture only, MockTransport below
    b.http_engine = get_engine()
    held = {}
    lock = threading.Lock()
    b.provider_checks = []
    def checked_out(connection, record, proxy):
        with lock: held[id(record)] = threading.get_ident()
    def checked_in(connection, record):
        with lock: held.pop(id(record), None)
    def provider_released():
        with lock:
            own = sum(owner == threading.get_ident() for owner in held.values())
            b.provider_checks.append({'thread':threading.get_ident(),'own_connections':own,
                                      'other_connections':len(held)-own})
        assert own == 0, 'calling operation retained a connection during provider IO'
    event.listen(b.http_engine.pool,'checkout',checked_out)
    event.listen(b.http_engine.pool,'checkin',checked_in)
    b.provider_released = provider_released
    client = httpx.Client
    def handle(request):
        # Browser resource requests may legitimately use this pool concurrently.
        # The calling operation must release its own connections before HTTP.
        b.provider_released()
        payload = json.loads(request.content)
        b.calls.append(payload)
        if b.action:
            response = b.action(request, payload)
            if response is not None:
                return response
        if 'documents' in payload:
            return httpx.Response(200, json={'model': 'accounts/fireworks/models/qwen3-reranker-8b',
                'provider': 'Fireworks', 'results': [{'index': i, 'relevance_score': 1-i/10}
                    for i in range(len(payload['documents']))], 'usage': {'total_tokens': 2}})
        return httpx.Response(200, json={'model': 'text-embedding-3-small', 'provider': 'Azure',
            'data': [{'embedding': [.1,.2,.3]}], 'usage': {'prompt_tokens': 2}})
    # indexed already patched Client; unwrap by explicitly using a captured base class.
    base_client = httpx._client.Client
    monkeypatch.setattr(httpx, 'Client', lambda *a, **kw: base_client(*a, **(kw | {'transport': httpx.MockTransport(handle)})))
    try:
        yield b
    finally:
        event.remove(b.http_engine.pool,'checkout',checked_out)
        event.remove(b.http_engine.pool,'checkin',checked_in)


def request(b, **body):
    return b.browser.post('/search', {'query': 'measured', 'mode': 'hybrid', 'rerank': True} | body)


def proof(b):
    with Session(b.api.engine) as db:
        return frozen(db.query(SearchUsage).order_by(SearchUsage.created_at).first())


@pytest.mark.parametrize('mode,rerank,n', [('keyword',False,0),('hybrid',False,1),('keyword',True,1),('hybrid',True,2)])
def test_real_transport_and_detached_response(ready, mode, rerank, n):
    b=ready
    response=request(b, mode=mode, rerank=rerank)
    assert response.status_code == 200, response.text
    assert response.json()['count'] >= 1 and len(b.calls) == n
    assert b.api.engine.pool.checkedout() == 0
    chunk=next(h['chunk_id'] for h in response.json()['hits'] if 'measured' in h['text'])
    opened=b.browser.get('/search/chunks/'+chunk)
    assert opened.status_code == 200 and 'measured' in opened.json()['text']
    assert b.api.engine.pool.checkedout() == 0
    assert all('arXiv:' not in str(p) for p in b.calls)
    if n:
        assert sql(b, 'SELECT bool_and(reservation_token IS NOT NULL AND operation_id IS NOT NULL AND session_id IS NOT NULL) FROM search_usage')


@pytest.mark.parametrize('phase', ['counter','reservation','reaper','settlement'])
@pytest.mark.parametrize('landed', [False,True])
def test_commit_exception_always_uncertain_no_next_provider(ready, monkeypatch, phase, landed):
    b=ready; original=Session.commit
    def fail(db):
        if db.info.get('search_phase') == phase:
            if landed: original(db)
            raise RuntimeError('private exception marker')
        return original(db)
    monkeypatch.setattr(Session,'commit',fail)
    response=request(b)
    assert response.status_code == 503 and response.json() == {'error':'search_commit_uncertain'}
    assert len(b.calls) == (1 if phase=='settlement' else 0)
    assert b.api.engine.pool.checkedout() == 0


@pytest.mark.parametrize('phase', ['entry','counter','reservation','presend','settlement','response','viewer'])
@pytest.mark.parametrize('method', ['rollback','close'])
def test_release_fault_stops_with_stable_json_and_invalidates(ready, monkeypatch, phase, method):
    b=ready
    chunk=str(sql(b,'SELECT chunk_id FROM index_entries LIMIT 1'))
    original=getattr(Session,method); invalidated=[]; invalidate=Session.invalidate
    def fail(db):
        if db.info.get('search_phase')==phase:
            original(db)
            raise RuntimeError('release marker')
        return original(db)
    def observed(db):
        invalidated.append(db.info.get('search_phase'))
        return invalidate(db)
    monkeypatch.setattr(Session,method,fail);monkeypatch.setattr(Session,'invalidate',observed)
    response=b.browser.get('/search/chunks/'+chunk) if phase=='viewer' else request(b)
    assert response.status_code==503 and response.json()=={'error':'search_release_unavailable'}
    assert phase in invalidated and b.api.engine.pool.checkedout()==0
    if phase in ('entry','counter','reservation','presend','viewer'): assert not b.calls
    if phase=='settlement': assert len(b.calls)==1


@pytest.mark.parametrize('change', ['epoch','generation','disable','logout','role','membership'])
def test_change_commits_while_provider_blocked_no_stale_response(ready, change):
    b=ready; entered, released=Event(),Event()
    collection=None
    if change=='membership':
        collection=b.browser.post('/collections',{'name':'C2d6'}).json()['collection']['id']
        assert b.browser.post('/collections/'+collection+'/items',{'item_id':str(b.item)}).status_code==201
    def blocked(request,payload):
        entered.set(); assert released.wait(15)
    b.action=blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(request,b,**({'collection_id':collection} if collection else {}))
        try:
            assert entered.wait(15)
            assert b.api.engine.pool.checkedout()==0
            if change=='disable': owner_status(b,'disabled')
            elif change=='logout': assert b.browser.post('/auth/logout',{}).status_code==204
            elif change=='role': sql(b,"UPDATE users SET role='admin' WHERE id=:u",u=b.uid)
            elif change=='membership': sql(b,'DELETE FROM collection_items WHERE collection_id=:c',c=uuid.UUID(collection))
            elif change=='epoch': sql(b,'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',i=b.item)
            else: sql(b,"UPDATE index_generations SET state='superseded' WHERE item_id=:i",i=b.item)
        finally: released.set()
        response=future.result(20)
    assert response.status_code == (401 if change in ('disable','logout') else 409),response.text
    assert len(b.calls)==1 and 'measured' not in response.text
    assert sql(b,"SELECT status FROM search_usage")=='completed'


@pytest.mark.parametrize('phase', ['retrieval','response','viewer'])
@pytest.mark.parametrize('ttl', ['idle','absolute'])
def test_final_clock_after_predicate_sql_including_empty(ready, phase, ttl):
    b=ready; changed=[]; predicates=[]
    chunk=str(sql(b,'SELECT chunk_id FROM index_entries LIMIT 1'))
    delta=b.settings.auth.session_idle if ttl=='idle' else b.settings.auth.session_absolute
    def advance(conn,cursor,statement,params,context,executemany):
        dbphase=conn.info.get('test_search_phase')
        if dbphase==phase and 'ras_auth_session_live' in statement:
            predicates.append(True)
            # Advance only after the LAST predicate of this phase; an earlier
            # caller check must not accidentally protect a broken final clock.
            if len(predicates)==({'retrieval':2,'response':3,'viewer':2}[phase]):
                changed.append(True);b.api.clock.now += delta
    def mark(db, transaction, conn): conn.info['test_search_phase']=db.info.get('search_phase')
    event.listen(Session,'after_begin',mark);event.listen(b.http_engine,'after_cursor_execute',advance)
    try:
        response=b.browser.get('/search/chunks/'+chunk) if phase=='viewer' else request(b,mode='keyword',rerank=False,query='the and')
        assert changed and response.status_code==401,response.text
        assert response.json()['error']=='session_expired'
    finally:
        event.remove(Session,'after_begin',mark);event.remove(b.http_engine,'after_cursor_execute',advance)


@pytest.mark.parametrize('field', money.FIELDS)
def test_nonce_tuple_cannot_renew_or_settle_another_attempt(ready, field):
    b=ready; assert request(b,rerank=False).status_code==200
    p=proof(b); values=dict(p.values); v=values[field]
    values[field] = (v+timedelta(days=1) if field=='day' else v+Decimal('.01') if field=='reserved_usd'
        else 'forged' if field in ('task_type','model_id','provider') else uuid.uuid4())
    forged=Record(values)
    assert money.renew(b.api.engine,forged,b.settings,clock=lambda:b.api.clock.now) is False
    with pytest.raises(ApiError,match='search_reservation_lost'):
        money.settle(b.api.engine,forged,b.settings,tokens=1,clock=lambda:b.api.clock.now)
    assert sql(b,'SELECT input_tokens FROM search_usage')==2


def test_expiry_old_day_late_duplicate_settlement_and_no_legacy_adoption(ready):
    b=ready
    def uncertain(*_): return httpx.Response(500)
    b.action=uncertain
    assert request(b,rerank=False).status_code==503
    p=proof(b)
    # A genuine completed HTTP timeout/500 remains financially owned after auth expires.
    b.api.clock.advance(days=1)
    assert not money.renew(b.api.engine,p,b.settings,clock=lambda:b.api.clock.now)
    try:
        first=money.settle(b.api.engine,p,b.settings,tokens=2,clock=lambda:b.api.clock.now)
    except ApiError as exc:
        first={'unexpected_error':exc.code}
    assert first.get('status')=='usage_price_estimate', first
    assert first==money.settle(b.api.engine,p,b.settings,tokens=999,clock=lambda:b.api.clock.now)
    assert sql(b,'SELECT cost_usd FROM search_usage')==legacy.money(2,b.settings.embedding.max_usd_per_million_tokens)
    with Session(b.api.engine) as db,pytest.raises(ApiError,match='search_reservation_binding_required'):
        legacy.settle(db,p.id,tokens=1,price=1)
    from app.rag_runtime import settle as settle_generation
    with pytest.raises(ApiError,match='rag_reservation_lost'):
        settle_generation(b.api.engine,p,b.settings,tokens=1,output_tokens=1)
    assert len(b.calls)==1


@pytest.mark.parametrize('phase',['presend','response'])
def test_new_proof_after_commit_not_precommit_dto(ready,monkeypatch,phase):
    b=ready; original=boundary.checkpoint
    def change(engine,name,operation,**kwargs):
        if name==phase: sql(b,'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',i=b.item)
        return original(engine,name,operation,**kwargs)
    monkeypatch.setattr(boundary,'checkpoint',change)
    response=request(b)
    assert response.status_code==409 and response.json()=={'error':'search_scope_changed'}
    assert len(b.calls)==(0 if phase=='presend' else 2)


def test_pending_real_gate_health_and_no_mutation(ready,monkeypatch):
    b=ready; monkeypatch.setattr(auth_safety,'cutover',lambda db:'auth_safety_pending')
    assert request(b).status_code==503
    assert b.browser.get('/healthz').status_code==200
    assert b.browser.get('/readyz').status_code==503
    assert not b.calls and sql(b,'SELECT count(*) FROM search_usage')==0


@pytest.mark.parametrize('role',['owner','recovery','grant'])
def test_runtime_login_and_each_grant_before_counter(ready,monkeypatch,role):
    b=ready
    if role=='owner': engine=b.owner
    elif role=='recovery':
        import os
        from app.db import make_engine
        engine=make_engine(b.owner.url.set(username='c1_recovery',password=os.environ['RAS_C1_RECOVERY_PASSWORD']))
    else:
        engine=b.api.engine;sql(b,'REVOKE UPDATE ON search_usage FROM c1_runtime')
    try:
        with pytest.raises(ApiError): boundary.entry(engine)
        assert sql(b,'SELECT count(*) FROM search_request_days')==0
        assert not b.calls
    finally:
        if role=='recovery':engine.dispose()


def test_document_query_share_slots_but_rerank_is_separate(ready):
    b=ready;b.settings.embedding.max_concurrent_requests=1
    sql(b,"UPDATE embedding_calls SET status='reserved',lease_until=:t WHERE chunk_hash=(SELECT chunk_hash FROM chunks WHERE text NOT LIKE '%measured%' LIMIT 1)",
        t=b.api.clock.now+timedelta(minutes=5))
    response=request(b,rerank=False)
    assert response.status_code==429 and response.json()['error']=='search_embedding_concurrency_limit'
    assert not b.calls
    response=request(b,mode='keyword')
    assert response.status_code==200 and response.json()['rerank_status']=='applied'
    assert len(b.calls)==1


@pytest.mark.parametrize('task', ['generation','embed_query','rerank'])
def test_app_total_keeps_all_tasks_and_unknown_ceilings(ready,task):
    b=ready
    sql(b,"""INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,reserved_usd,cost_status)
        VALUES (:u,:d,:task,'historic','openrouter','uncertain',:amount,'unknown_reserved_ceiling')""",
        u=b.uid,d=b.api.clock.now.date(),task=task,amount=Decimal(str(b.settings.budget.app_daily_usd)))
    response=request(b,rerank=False)
    assert response.status_code==429 and response.json()['error']=='search_app_budget_exhausted'
    assert not b.calls


def test_expired_nonce_reaper_keeps_money_and_no_lease_resurrection(ready):
    b=ready;b.action=lambda *_:httpx.Response(500)
    assert request(b,rerank=False).status_code==503
    p=proof(b)
    sql(b,"UPDATE search_usage SET status='reserved',lease_until=:t WHERE id=:i",i=p.id,t=b.api.clock.now-timedelta(seconds=1))
    assert not money.renew(b.api.engine,p,b.settings,clock=lambda:b.api.clock.now)
    assert money.reap(b.api.engine,b.settings,clock=lambda:b.api.clock.now)==1
    assert sql(b,"SELECT status FROM search_usage")=='uncertain'
    assert sql(b,'SELECT reserved_usd FROM search_usage')==p.reserved_usd
    money.settle(b.api.engine,p,b.settings,tokens=2,clock=lambda:b.api.clock.now)
    assert sql(b,'SELECT day FROM search_usage')==p.day
    assert not money.renew(b.api.engine,p,b.settings,clock=lambda:b.api.clock.now)
    assert len(b.calls)==1


def test_heartbeat_failure_financially_settles_but_never_returns_content(ready,monkeypatch):
    b=ready;called=Event();b.settings.embedding.reservation_lease_seconds=3
    def stop(*a,**kw):called.set();return False
    monkeypatch.setattr(money,'renew',stop)
    def block(*_):assert called.wait(10)
    b.action=block
    response=request(b)
    assert response.status_code==503 and response.json()['error']=='search_heartbeat_unavailable'
    assert sql(b,"SELECT status FROM search_usage")=='completed'
    assert len(b.calls)==1 and b.api.engine.pool.checkedout()==0


@pytest.mark.parametrize('malformed', ['identity','dimension','nan','usage','mapping','timeout'])
def test_malformed_transport_retains_ceiling_without_resend(ready,malformed):
    b=ready
    def fail(req,payload):
        if malformed=='timeout':raise httpx.ReadTimeout('fixture',request=req)
        if malformed=='mapping':
            return httpx.Response(200,json={'model':'accounts/fireworks/models/qwen3-reranker-8b','provider':'Fireworks',
                'results':[{'index':999,'relevance_score':1}], 'usage':{'total_tokens':2}})
        return httpx.Response(200,content=json.dumps({'model':'wrong' if malformed=='identity' else 'text-embedding-3-small',
            'provider':'Azure','data':[{'embedding':[.1] if malformed=='dimension' else [float('inf')] if malformed=='nan' else [.1,.2,.3]}],
            'usage':{'prompt_tokens':100000 if malformed=='usage' else 2}}))
    b.action=fail
    response=request(b,mode='keyword' if malformed=='mapping' else 'hybrid')
    if malformed=='mapping':assert response.status_code==200 and response.json()['rerank_status']=='degraded_rerank_response_invalid'
    else:assert response.status_code==503,response.text
    assert len(b.calls)==1 and sql(b,"SELECT status FROM search_usage")=='uncertain'
    assert sql(b,'SELECT cost_usd IS NULL AND reserved_usd>0 FROM search_usage')


def test_empty_response_waiting_on_journal_uses_new_clock(ready,monkeypatch):
    import time
    b=ready;entered,proceed=Event(),Event();original=auth_safety.require_final
    def paused(db,**kw):
        if db.info.get('search_phase')=='response':
            entered.set();assert proceed.wait(15)
        return original(db,**kw)
    monkeypatch.setattr(auth_safety,'require_final',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(request,b,mode='keyword',rerank=False,query='the and')
        assert entered.wait(15)
        with b.owner.connect() as conn:
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':recovery.JOURNAL_LOCK})
            proceed.set()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                waiting=sql(b,"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'")
                if waiting:break
                time.sleep(.01)
            assert waiting,'search did not wait on the real journal lock'
            b.api.clock.now += b.settings.auth.session_idle
            conn.rollback()
        response=future.result(15)
    assert response.status_code==401 and response.json()['error']=='session_expired'
    assert not b.calls and b.http_engine.pool.checkedout()==0


def test_no_terms_still_counts_and_checks_caller_and_csrf(ready):
    b=ready;b.settings.search.max_requests_per_user_day=1
    response=request(b,query='the and',mode='keyword',rerank=False)
    assert response.status_code==200 and response.json()['keyword_query']['status']=='no_terms'
    assert not b.calls
    response=request(b,mode='keyword')
    assert response.status_code==429 and response.json()['error']=='search_request_limit'
    assert b.browser.post('/search',{'query':'x','mode':'keyword'},csrf=False).status_code==403


def test_cancellation_keeps_owned_reservation_and_stops_heartbeat(ready,monkeypatch):
    import threading
    b=ready
    sid=sql(b,'SELECT id FROM sessions WHERE user_id=:u ORDER BY created_at DESC LIMIT 1',u=b.uid)
    principal=Principal(b.uid,'user',sid,'worker@example.test',None)
    class Cancelled(BaseException):pass
    def cancel(*a,**kw):raise Cancelled()
    monkeypatch.setattr(legacy,'embed',cancel)
    with pytest.raises(Cancelled):
        search.search(b.api.engine,principal,b.settings,'measured','hybrid',None,False,None,
            clock=lambda:b.api.clock.now,csrf=b.browser.client.cookies.get('ras_csrf'))
    assert sql(b,"SELECT status FROM search_usage")=='reserved'
    assert sql(b,'SELECT cost_usd IS NULL AND reserved_usd>0 FROM search_usage')
    assert not any(t.name=='search-reservation-heartbeat' for t in threading.enumerate())
    assert b.api.engine.pool.checkedout()==0 and not b.calls


def test_schema_binding_rejects_partial_and_duplicate_nonce(ready):
    b=ready
    assert request(b,rerank=False).status_code==200
    p=proof(b)
    # DB constraints protect direct runtime writes too, independently of Python matches().
    for statement in (
        "UPDATE search_usage SET operation_id=NULL WHERE id=:i",
        """INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,reserved_usd,
            cost_status,reservation_token,operation_id,session_id)
            SELECT user_id,day,task_type,model_id,provider,status,reserved_usd,cost_status,
                reservation_token,operation_id,session_id FROM search_usage WHERE id=:i""",
    ):
        with pytest.raises(IntegrityError):
            with b.api.engine.begin() as conn:
                conn.execute(text(statement),{'i':p.id})
    assert sql(b,'SELECT count(*) FROM search_usage')==1
    assert len(b.calls)==1
