"""Two JSON routes, runtime PostgreSQL/journal and real auth lifecycle.

Shared business setup uses the real C4 contract; pending HTTP has no patch. Faults
and corruption are synthetic and retain real SQL/head/receipt/clock guards.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app import auth_safety, recovery
from app.api import upload_intents
from app.api.errors import ApiError
from app.api.routes import uploads
from app.config import get_settings
from app.db import get_engine
from app.identity import final_caller as callers
from app.identity.login import login
from app.identity.predicates import lock_users
from app.identity.tokens import hash_token
from app.main import app
from app.models import UploadIntent, ProcessingAcknowledgement
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as world, issue, accept, EMAIL, PASSWORD
from tests.test_c2d1_final_caller import browser, mutate, close, wait_for

NOTICE = '/uploads/notice'
INTENTS = '/uploads/intents'
TABLES = ('processing_acknowledgements', 'upload_intents')
DERIVED = ('library_items', 'paper_versions', 'source_files', 'ingestion_jobs', 'chunks', 'index_generations')


@pytest.fixture
def consent(browser, monkeypatch):
    b = browser
    b.admin_id, b.admin_actor = b.w.uid, b.w.actor
    user = accept(b.w, issue(b.w))
    with Session(b.w.runtime) as db:
        actor = login(db, email=EMAIL, password=PASSWORD, now=NOW, settings=b.w.settings, clock=b.clock)[1]
    b.w.uid, b.w.actor = user.id, actor
    b.client.cookies.set('ras_session', actor.token)
    b.client.cookies.set('ras_csrf', actor.csrf_token)
    monkeypatch.setenv('RAS_FILES__UPLOAD_INTENT_LIFETIME', 'PT2M')
    get_settings.cache_clear()
    b.body = dict(acknowledged=True, notice_version=get_settings().upload_notice_version,
        file_kind='markdown', filename='synthetic-private-name.md', byte_size=19, sha256='a'*64)
    return b


def request(b, body=None, headers=None):
    return b.client.post(INTENTS, json=b.body if body is None else body,
        headers=headers if headers is not None else {'Origin':'http://web.test', 'X-CSRF-Token':b.w.actor.csrf_token})


def state(b):
    with b.w.engine.connect() as conn:
        rows = {t: conn.execute(text(f'SELECT row_to_json(t)::text FROM {t} t ORDER BY 1')).scalars().all() for t in TABLES}
        rows['audit'] = conn.execute(text("SELECT row_to_json(t)::text FROM audit_events t WHERE action='upload.intent.create' ORDER BY 1")).scalars().all()
        return rows


def clean():
    assert get_engine().pool.checkedout() == 0


def alter(b, change):
    if change in ('role', 'csrf'):
        with b.w.engine.begin() as conn:
            conn.execute(text("UPDATE users SET role='admin' WHERE id=:u") if change=='role' else
                text("UPDATE sessions SET csrf_token_hash=repeat('f',64) WHERE id=:s"), {'u':b.w.uid,'s':b.w.actor.session_id})
    else:
        mutate(b, change)


@pytest.mark.parametrize('kind,name', [('pdf','paper.PDF'),('markdown','paper.markdown')])
def test_success_exact_binding_hash_fresh_transactions_flush_locks_no_io(consent, monkeypatch, caplog, kind, name):
    b = consent; before = b.w.path.read_bytes(); guards=[]; sql=[]; real = auth_safety.require_final
    def gate(db, **kw):
        assert not db.new and not db.dirty and not db.deleted
        result=real(db, **kw); guards.append((db.get_transaction(),kw.get('writer',False)))
        return result
    monkeypatch.setattr(auth_safety,'require_final',gate)
    def trace(conn,cursor,statement,parameters,context,executemany): sql.append(statement.lower())
    event.listen(get_engine(),'before_cursor_execute',trace)
    try:
        r=request(b,b.body|{'file_kind':kind,'filename':name})
        assert r.status_code==201, r.text
    finally: event.remove(get_engine(),'before_cursor_execute',trace)
    assert [w for _,w in guards]==[True,False] and guards[0][0] is not guards[1][0]
    assert all(not tx.is_active for tx,_ in guards)
    exclusive=next(i for i,s in enumerate(sql) if 'pg_advisory_xact_lock(' in s)
    shared=next(i for i,s in enumerate(sql) if 'pg_advisory_xact_lock_shared' in s and i>exclusive)
    assert any('ras_auth_lock_users' in s for s in sql[:exclusive])
    response_lock=next(i for i,s in enumerate(sql) if s.startswith('select upload_intents.id') and 'for share' in s)
    assert not any('insert ' in s or 'update ' in s or 'for share' in s or 'for update' in s for s in sql[exclusive+1:response_lock])
    assert any(s.startswith('select processing_acknowledgements.id') and 'for share' in s for s in sql[response_lock:shared])
    assert not any('for share' in s or 'for update' in s or 'insert ' in s or 'update ' in s for s in sql[shared+1:])
    with Session(b.w.runtime) as db:
        i=db.scalar(select(UploadIntent)); a=db.scalar(select(ProcessingAcknowledgement))
        assert (i.user_id,i.session_id,i.acknowledgement_id)==(a.actor_user_id,a.session_id,a.id)==(b.w.uid,b.w.actor.session_id,a.id)
        assert i.notice_version==a.notice_version==b.body['notice_version']
        assert (i.created_at,a.acknowledged_at,i.expires_at)==(NOW,NOW,NOW+timedelta(minutes=2))
        assert i.token_hash==hash_token(r.json()['intent_token']) and i.used_at is None and i.item_id is None
        assert (i.file_kind,i.filename,i.declared_size,i.declared_sha256)==(kind,name,19,'a'*64)
        assert all(db.scalar(text(f'SELECT count(*) FROM {t}'))==0 for t in DERIVED)
        dump=' '.join(db.execute(text('SELECT row_to_json(t)::text FROM audit_events t')).scalars())
    assert set(r.json())=={'intent_token','expires_at'} and b.w.path.read_bytes()==before
    for secret in (r.json()['intent_token'],b.w.actor.token,b.w.actor.csrf_token,name):
        assert secret not in caplog.text+dump+b.w.path.read_text()
    clean()


def test_notice_user_admin_anonymous_and_two_separate_confirmations(consent):
    b=consent
    for actor in (b.w.actor,b.admin_actor):
        b.client.cookies.set('ras_session',actor.token)
        r=b.client.get(NOTICE)
        assert r.status_code==200 and set(r.json())=={'version','text'}
        assert all(s in r.json()['text'] for s in ('OpenRouter/provider','embedding','rerank','RAG'))
    assert state(b)=={'processing_acknowledgements':[],'upload_intents':[],'audit':[]}
    b.client.cookies.set('ras_csrf',b.admin_actor.csrf_token)
    assert request(b,headers={'Origin':'http://web.test','X-CSRF-Token':b.admin_actor.csrf_token}).status_code==403
    for change in ({'notice_version':'old'},{'filename':'../x.md'},{'byte_size':3000000}):
        assert request(b,b.body|change,headers={'Origin':'http://web.test','X-CSRF-Token':b.admin_actor.csrf_token}).status_code==403
    b.client.cookies.clear()
    assert b.client.get(NOTICE).status_code==401
    b.client.cookies.set('ras_csrf',b.w.actor.csrf_token)
    assert request(b).status_code==401
    b.client.cookies.set('ras_session',b.w.actor.token); b.client.cookies.set('ras_csrf',b.w.actor.csrf_token)
    a=request(b); c=request(b)
    assert a.status_code==c.status_code==201
    assert a.json()['intent_token']!=c.json()['intent_token']
    assert all(len(rows)==2 for rows in state(b).values())
    clean()


@pytest.mark.parametrize('value',[False,None,0,1,'true','1',[],{}])
def test_only_json_boolean_true(consent,value):
    b=consent
    r=request(b,b.body|{'acknowledged':value})
    assert r.status_code==422 and not state(b)['upload_intents']
    assert 'input' not in r.text
    clean()


@pytest.mark.parametrize('change,expected', [({'acknowledged':None},422),({'notice_version':'old'},409),
    ({'filename':'../x.md'},422),({'filename':'x\\x.md'},422),({'filename':'x\x00.md'},422),
    ({'filename':'x.pdf'},422),({'file_kind':'text'},422),({'filename':''},422),
    ({'byte_size':0},422),({'byte_size':-1},422),({'byte_size':3000000},413),
    ({'sha256':'A'*64},422),({'sha256':'a'*63},422),({'owner_user_id':str(uuid.uuid4())},422),
    ({'session_id':str(uuid.uuid4())},422),({'file':'bytes'},422)])
def test_metadata_contract_denied_without_rows(consent,change,expected):
    b=consent; r=request(b,b.body|change)
    assert r.status_code==expected, r.text
    assert not any(state(b).values())


def test_missing_acknowledgement_and_size_config(consent,monkeypatch):
    b=consent; body=dict(b.body); del body['acknowledged']
    assert request(b,body).status_code==422
    monkeypatch.setenv('RAS_FILES__MARKDOWN_MAX_BYTES','18'); get_settings.cache_clear()
    assert request(b).status_code==413 and not any(state(b).values())


@pytest.mark.parametrize('headers', [[],[('Origin','http://evil.test')],
    [('Origin','http://web.test'),('Origin','http://web.test')]])
def test_origin_missing_wrong_duplicate(consent,headers):
    b=consent
    r=request(b,headers=headers+[('X-CSRF-Token',b.w.actor.csrf_token)])
    assert r.status_code==403 and r.json()=={'error':'csrf_failed'}
    assert not any(state(b).values())


@pytest.mark.parametrize('route',[NOTICE,INTENTS])
@pytest.mark.parametrize('change',['revoke','disable','generation','quarantine','held','receipt_credential',
    'receipt_status','wrong_receipt','head','latest_credential','latest_status','hold','ahead','unconfirmed'])
def test_real_changes_after_dependency(consent,route,change):
    b=consent; b.after_dependency=lambda db,ctx: alter(b,change)
    r=b.client.get(route) if route==NOTICE else request(b)
    assert r.status_code==(503 if change in ('hold','ahead','unconfirmed','receipt_credential','receipt_status','wrong_receipt','head','latest_credential','latest_status') else 401), r.text
    assert 'intent_token' not in r.text and 'text' not in r.json() and not any(state(b).values())
    if r.status_code==401: assert 'Max-Age=0' in r.headers['set-cookie']
    clean()


@pytest.mark.parametrize('change',['role','csrf','owner'])
def test_current_role_csrf_and_exact_owner(consent,change):
    b=consent
    def changed(db,ctx):
        if change=='owner':
            with b.w.engine.begin() as conn:
                conn.execute(text("SET LOCAL session_replication_role='replica'"))
                conn.execute(text('UPDATE sessions SET user_id=:u,credential_generation=(SELECT credential_generation FROM users WHERE id=:u) WHERE id=:s'),{'u':b.admin_id,'s':ctx.session_id})
        else: alter(b,change)
    b.after_dependency=changed
    r=request(b)
    assert r.status_code==(401 if change=='owner' else 403) and not any(state(b).values())


@pytest.mark.parametrize('phase,kind',[(p,k) for p in ('notice','pre','post') for k in ('idle','absolute','intent') if (p,k)!=('notice','intent')])
@pytest.mark.parametrize('offset',[-1,0,1])
def test_final_predicate_sql_crosses_expiry(consent,monkeypatch,phase,kind,offset):
    b=consent
    end=NOW+timedelta(seconds=90 if kind!='intent' else 120)
    if kind=='idle':
        monkeypatch.setenv('RAS_AUTH__SESSION_IDLE','PT90S'); get_settings.cache_clear()
    if kind=='absolute':
        with b.w.engine.begin() as conn:
            conn.execute(text('UPDATE sessions SET absolute_expires_at=:e WHERE id=:s'),{'e':end,'s':b.w.actor.session_id})
    real_check=upload_intents._check; checks=[]
    def check(*args):
        checks.append(args[0].get_transaction()); return real_check(*args)
    monkeypatch.setattr(upload_intents,'_check',check)
    module=callers if phase=='notice' else upload_intents; real=module.session_live; crossed=[]
    def live(db,*a,**kw):
        result=real(db,*a,**kw)
        if phase=='notice' or len(checks)==(1 if phase=='pre' else 2):
            assert result; b.at=end+timedelta(microseconds=offset); crossed.append(True)
        return result
    monkeypatch.setattr(module,'session_live',live)
    r=b.client.get(NOTICE) if phase=='notice' else request(b)
    assert crossed
    assert r.status_code==((200 if phase=='notice' else 201) if offset<0 else (409 if kind=='intent' else 401)), r.text
    assert len(state(b)['upload_intents'])==(0 if phase=='notice' or (phase=='pre' and offset>=0) else 1)
    if offset>=0: assert 'intent_token' not in r.text
    clean()


@pytest.mark.parametrize('phase',['pre','post'])
@pytest.mark.parametrize('field',['user_id','session_id','notice_version','filename','declared_size',
    'declared_sha256','file_kind','token_hash','acknowledgement_id','used_at','item_id',
    'created_at','expires_at','ack_user','ack_session','ack_notice','ack_time'])
def test_binding_reread_after_flush_and_known_commit(consent,monkeypatch,phase,field):
    b=consent; real=upload_intents._check; calls=[]
    def check(db,expected,*args):
        calls.append(1)
        if len(calls)==(1 if phase=='pre' else 2):
            # Controlled corruption in the actual transaction, before the real
            # recheck. No production grants change; runtime can write these
            # business tables. Invalid IDs refer to existing fixture rows.
            if field.startswith('ack_'):
                col={'ack_user':'actor_user_id','ack_session':'session_id','ack_notice':'notice_version','ack_time':'acknowledged_at'}[field]
                value=b.admin_id if field=='ack_user' else b.admin_actor.session_id if field=='ack_session' else 'old' if field=='ack_notice' else NOW+timedelta(seconds=1)
                db.execute(text(f'UPDATE processing_acknowledgements SET {col}=:v WHERE id=:id'),{'v':value,'id':expected.acknowledgement_id})
            else:
                values={'user_id':b.admin_id,'session_id':b.admin_actor.session_id,'notice_version':'old',
                    'filename':'other.md','declared_size':20,'declared_sha256':'b'*64,'file_kind':'pdf','token_hash':'c'*64,
                    'used_at':NOW,'created_at':NOW-timedelta(seconds=1),'expires_at':NOW+timedelta(minutes=3)}
                if field=='acknowledgement_id':
                    value=db.scalar(text('INSERT INTO processing_acknowledgements(actor_user_id,session_id,notice_version,acknowledged_at) VALUES(:u,:s,:n,:at) RETURNING id'),{'u':b.w.uid,'s':b.w.actor.session_id,'n':b.body['notice_version'],'at':NOW})
                elif field=='item_id':
                    value=db.scalar(text("INSERT INTO library_items(visibility,owner_user_id,created_by_user_id) VALUES('private',:u,:u) RETURNING id"),{'u':b.w.uid})
                else: value=values[field]
                db.execute(text(f'UPDATE upload_intents SET {field}=:v WHERE id=:id'),{'v':value,'id':expected.intent_id})
            # Injection DML is not a production lock map. Keep this fault
            # distinct from the success SQL trace and external auth races.
        return real(db,expected,*args)
    monkeypatch.setattr(upload_intents,'_check',check)
    r=request(b)
    assert r.status_code==409 and r.json()=={'error':'upload_intent_changed'},r.text
    assert len(state(b)['upload_intents'])==(0 if phase=='pre' else 1)
    clean()


@pytest.mark.parametrize('phase,change',[(p,c) for p in ('pre','post') for c in ('generation','receipt_status','head','quarantine','held','csrf','role','hold','bad_journal') if p=='post' or c in ('generation','csrf','hold','bad_journal')])
def test_final_actor_guard_real_after_flush_or_commit(consent,monkeypatch,phase,change):
    b=consent; real=auth_safety.require_final
    def gate(db,**kw):
        if bool(kw.get('writer'))==(phase=='pre'):
            if change=='bad_journal': b.w.path.write_bytes(b.w.path.read_bytes()+b'broken\n')
            elif change=='hold': recovery.hold(b.w.engine,b.w.settings,NOW)
            else: alter(b,change)
        return real(db,**kw)
    monkeypatch.setattr(auth_safety,'require_final',gate)
    r=request(b)
    expected=503 if change in ('hold','bad_journal','receipt_status','head') else 403 if change in ('role','csrf') else 401
    assert r.status_code==expected and 'intent_token' not in r.text
    assert len(state(b)['upload_intents'])==(0 if phase=='pre' else 1)
    clean()


@pytest.mark.parametrize('landed',[False,True])
def test_commit_uncertain_no_token_no_retry(consent,monkeypatch,landed):
    b=consent; real=Session.commit; calls=[]
    def commit(db):
        if db.get_bind() is get_engine():
            calls.append(1)
            if len(calls)==2:
                real(db) if landed else db.rollback()
                raise RuntimeError('secret-driver-error')
        return real(db)
    monkeypatch.setattr(Session,'commit',commit)
    r=request(b)
    assert r.status_code==503 and r.json()=={'error':'upload_intent_outcome_uncertain'}
    assert len(calls)==2 and len(state(b)['upload_intents'])==int(landed)
    clean()


@pytest.mark.parametrize('fault',['flush','pre_sql','post_sql','post_revoke','post_disable'])
def test_controlled_failures_and_landed_rows(consent,monkeypatch,fault):
    b=consent
    if fault=='flush':
        real=Session.flush
        def flush(db,*a,**kw):
            if any(isinstance(x,UploadIntent) for x in db.new): raise RuntimeError('secret INSERT')
            return real(db,*a,**kw)
        monkeypatch.setattr(Session,'flush',flush)
    elif fault in ('pre_sql','post_sql'):
        real=upload_intents._check; calls=[]
        def check(db,*a):
            calls.append(1)
            if len(calls)==(1 if fault=='pre_sql' else 2): db.execute(text('SELECT synthetic_missing_function()'))
            return real(db,*a)
        monkeypatch.setattr(upload_intents,'_check',check)
    else:
        real=Session.commit; calls=[]
        def commit(db):
            result=real(db)
            if db.get_bind() is get_engine():
                calls.append(1)
                if len(calls)==2: alter(b,'revoke' if fault=='post_revoke' else 'disable')
            return result
        monkeypatch.setattr(Session,'commit',commit)
    r=request(b)
    code='session_expired' if fault.startswith('post_') and fault!='post_sql' else 'upload_intent_response_unavailable' if fault=='post_sql' else 'upload_intent_unavailable'
    assert r.status_code==(401 if code=='session_expired' else 503) and r.json()=={'error':code},r.text
    assert len(state(b)['upload_intents'])==int(fault.startswith('post_'))
    clean()


@pytest.mark.parametrize('route',[NOTICE,INTENTS])
def test_pending_http_unpatched_no_business_rows(installation,route):
    w=installation
    with TestClient(app) as client:
        r=client.get(route) if route==NOTICE else client.post(route,json={},headers={'Origin':'http://web.test'})
        assert r.status_code==503 and client.get('/healthz').status_code==200 and client.get('/readyz').status_code==503
    with w.runtime.connect() as conn:
        assert all(conn.scalar(text(f'SELECT count(*) FROM {t}'))==0 for t in TABLES+DERIVED)
    clean()


def test_timestamps_after_dependency_and_current_user_role(consent):
    b=consent
    b.after_dependency=lambda db,ctx: setattr(b,'at',NOW+timedelta(seconds=7))
    r=request(b)
    assert r.status_code==201
    with b.w.runtime.connect() as conn:
        row=conn.execute(text('SELECT i.created_at,i.expires_at,a.acknowledged_at FROM upload_intents i JOIN processing_acknowledgements a ON a.id=i.acknowledgement_id')).one()
    assert tuple(row)==(b.at,b.at+timedelta(minutes=2),b.at)


def test_cross_user_and_other_session_cannot_satisfy_secret_binding(consent):
    from app.authz import Principal
    b=consent; assert request(b).status_code==201
    uid,actor=b.w.uid,b.w.actor
    b.w.uid,b.w.actor=b.admin_id,b.admin_actor
    other=accept(b.w,issue(b.w,email='b-upload@example.test'))
    with Session(b.w.runtime) as db:
        other_actor=login(db,email='b-upload@example.test',password=PASSWORD,now=NOW,settings=b.w.settings,clock=b.clock)[1]
    b.w.uid,b.w.actor=uid,actor
    with Session(b.w.runtime) as db:
        i=db.scalar(select(UploadIntent))
        expected=upload_intents.Binding(i.id,i.acknowledgement_id,i.user_id,i.session_id,i.notice_version,
            i.file_kind,i.filename,i.declared_size,i.declared_sha256,i.token_hash,i.created_at,i.expires_at)
        for p in (Principal(b.admin_id,'admin',b.admin_actor.session_id,'',None),
                  Principal(b.w.uid,'user',b.admin_actor.session_id,'',None),
                  Principal(other.id,'user',other_actor.session_id,'',None)):
            with pytest.raises(ApiError,match='upload_intent_changed'):
                upload_intents._check(db,expected,p,b.w.actor.csrf_token,b.clock,get_settings())
        db.rollback()
    assert len(state(b)['upload_intents'])==1


@pytest.mark.parametrize('lock_kind',['user','journal'])
def test_lock_timeout_denies_without_business_rows(consent,lock_kind):
    b=consent; ready=threading.Event(); release=threading.Event()
    def pause(db,ctx): ready.set(); assert release.wait(10)
    b.after_dependency=pause
    with ThreadPoolExecutor(1) as pool:
        writing=pool.submit(request,b); assert ready.wait(10)
        with Session(b.w.runtime) as blocker:
            if lock_kind=='user': lock_users(blocker,[b.w.uid])
            else: blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'),{'k':recovery.JOURNAL_LOCK})
            release.set(); wait_for(b.w,'transactionid' if lock_kind=='user' else 'advisory')
            r=writing.result(15)
    assert r.status_code==503 and r.json()=={'error':'upload_intent_unavailable' if lock_kind=='user' else 'service_recovering'}
    assert not any(state(b).values())
    clean()


def test_raw_token_hash_mismatch_postcommit_never_returns_secret(consent,monkeypatch):
    b=consent; generate=upload_intents.generate_token
    def mismatched():
        raw,_=generate(); return raw,'f'*64
    monkeypatch.setattr(upload_intents,'generate_token',mismatched)
    r=request(b)
    assert r.status_code==409 and r.json()=={'error':'upload_intent_changed'}
    assert len(state(b)['upload_intents'])==1
    clean()


@pytest.mark.parametrize('lock_kind',['user','journal'])
@pytest.mark.parametrize('expired',[False,True])
def test_observed_wait_clock_and_pool(consent,lock_kind,expired):
    b=consent; dep=threading.Event(); proceed=threading.Event()
    def pause(db,ctx): dep.set(); assert proceed.wait(10)
    b.after_dependency=pause
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(request,b); assert dep.wait(10)
        with Session(b.w.runtime) as blocker:
            if lock_kind=='user': lock_users(blocker,[b.w.uid])
            else: blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'),{'k':recovery.JOURNAL_LOCK})
            proceed.set()
            try:
                wait_for(b.w,'transactionid' if lock_kind=='user' else 'advisory')
                # For a journal wait the intent already exists, so its own TTL
                # expires first. A User wait must not give caller new activity.
                b.at=NOW+timedelta(seconds=(1800 if expired else 1799) if lock_kind=='user' else (120 if expired else 119))
            finally: blocker.rollback()
        r=future.result(15)
    expected=(401 if lock_kind=='user' else 409) if expired else 201
    assert r.status_code==expected,r.text
    assert len(state(b)['upload_intents'])==int(not expired)
    clean()


@pytest.mark.parametrize('winner',['auth','writer'])
def test_auth_writer_two_legal_lock_orders(consent,monkeypatch,winner):
    b=consent; reached=threading.Event(); release=threading.Event(); real=auth_safety.require_final
    if winner=='writer':
        def gate(db,**kw):
            if kw.get('writer') and db.get_bind() is get_engine():
                reached.set(); assert release.wait(10)
            return real(db,**kw)
        monkeypatch.setattr(auth_safety,'require_final',gate)
        with ThreadPoolExecutor(2) as pool:
            writing=pool.submit(request,b); assert reached.wait(10)
            closing=pool.submit(close,b.w)
            try: wait_for(b.w,'transactionid')
            finally: release.set()
            r=writing.result(15); closing.result(15)
        assert r.status_code in (201,401),r.text  # either fresh response ordering
        assert len(state(b)['upload_intents'])==1
    else:
        def pause(db,ctx): reached.set(); assert release.wait(10)
        b.after_dependency=pause
        with ThreadPoolExecutor(1) as pool:
            writing=pool.submit(request,b); assert reached.wait(10)
            try: close(b.w)
            finally: release.set()
            r=writing.result(15)
        assert r.status_code==401 and not any(state(b).values())
    b.after_dependency=lambda db,ctx: None
    assert b.client.get(NOTICE).status_code==401
    clean()


@pytest.mark.parametrize('winner',['response','claim'])
def test_intent_claim_lock_order_and_unused_response(consent,monkeypatch,winner):
    b=consent; ready=threading.Event(); release=threading.Event(); real=auth_safety.require_final
    captured=[]
    def gate(db,**kw):
        if not kw.get('writer') and db.get_bind() is get_engine():
            with b.w.engine.connect() as conn:
                captured.append(conn.scalar(text('SELECT id FROM upload_intents')))
            if winner=='response':
                # The response resource SHARE locks have already been taken.
                ready.set(); assert release.wait(10)
            else:
                # Force the claim to win BEFORE response resource locks by
                # pausing right after the business commit below instead.
                pass
        return real(db,**kw)
    monkeypatch.setattr(auth_safety,'require_final',gate)
    if winner=='claim':
        commit=Session.commit; calls=[]
        def committed(db):
            result=commit(db)
            if db.get_bind() is get_engine():
                calls.append(1)
                if len(calls)==2:
                    with b.w.engine.connect() as conn: captured.append(conn.scalar(text('SELECT id FROM upload_intents')))
                    ready.set(); assert release.wait(10)
            return result
        monkeypatch.setattr(Session,'commit',committed)
    def claim():
        with Session(b.w.runtime) as db:
            db.execute(select(UploadIntent.id).where(UploadIntent.id==captured[0]).with_for_update()).all()
            db.execute(text('UPDATE upload_intents SET used_at=:at WHERE id=:id'),{'at':NOW,'id':captured[0]})
            db.commit()
    with ThreadPoolExecutor(2) as pool:
        writing=pool.submit(request,b); assert ready.wait(10)
        claiming=pool.submit(claim)
        if winner=='response':
            try: wait_for(b.w,'transactionid')
            finally: release.set()
        else:
            try: claiming.result(10)
            finally: release.set()
        r=writing.result(15); claiming.result(15)
    assert r.status_code==(201 if winner=='response' else 409),r.text
    assert len(state(b)['upload_intents'])==1
    clean()
