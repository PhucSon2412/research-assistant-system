"""Runtime auth + PostgreSQL/MinIO/journal, with controlled fault boundaries.

Only fixture cutover is scoped; production pending has a separate unpatched test.
All storage faults below are injections over real local MinIO, not crash tests.
"""
import asyncio
import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session
from starlette.requests import Request, ClientDisconnect

from app import auth_safety
from app.api import upload_original as original
from app.api.errors import ApiError
from app.api.routes import uploads
from app.config import get_settings, FileLimits, ObjectStorage
from app.db import get_engine
from app.identity.login import login
from app.identity.tokens import hash_token
from app.main import app
from app.models import UploadIntent, SourceFile, LibraryItem, IngestionJob
from app.storage import get_storage
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import world, issue, accept, EMAIL, PASSWORD
from tests.test_c2d1_final_caller import browser, mutate, close, wait_for
from tests.test_c2d4a_upload_intents import consent, request as create_intent, alter, clean
from tests.test_uploads import pdf


@pytest.fixture
def upload(consent, monkeypatch):
    b = consent
    # Explicit test env REQUIRED. This fixture never falls back to .env.
    assert all(os.environ.get(k) for k in ('MINIO_ROOT_USER', 'MINIO_ROOT_PASSWORD', 'MINIO_API_HOST_PORT'))
    monkeypatch.setenv('RAS_STORAGE__ENDPOINT_URL', 'http://127.0.0.1:' + os.environ['MINIO_API_HOST_PORT'])
    monkeypatch.setenv('RAS_STORAGE__ACCESS_KEY', os.environ['MINIO_ROOT_USER'])
    monkeypatch.setenv('RAS_STORAGE__SECRET_KEY', os.environ['MINIO_ROOT_PASSWORD'])
    bucket = 'ras-c2d4b-' + uuid.uuid4().hex
    monkeypatch.setenv('RAS_STORAGE__BUCKET', bucket)
    get_settings.cache_clear()
    b.storage = get_storage(get_settings()); b.storage.ensure_bucket()
    app.dependency_overrides[uploads._storage] = lambda: b.storage
    b.data = b'# Synthetic\n\nPrivate body.\n'
    b.make = lambda data=None, name='synthetic.md': create_intent(b, b.body | {
        'byte_size': len(b.data if data is None else data),
        'sha256': hashlib.sha256(b.data if data is None else data).hexdigest(),
        'file_kind': 'pdf' if name.endswith('.pdf') else 'markdown', 'filename': name}).json()['intent_token']
    yield b
    assert not original._attempts and original.slots.active == 0
    for key, _ in b.storage.orphan_keys(''): b.storage.delete(key)
    b.storage.client.delete_bucket(Bucket=bucket)
    clean()


def headers(b, token):
    return {'Origin': 'http://web.test', 'X-CSRF-Token': b.w.actor.csrf_token,
        'X-Upload-Intent': token, 'Content-Type': 'application/octet-stream'}


def send(b, token, data=None, extra=None):
    return b.client.put('/uploads/original', content=b.data if data is None else data,
        headers=headers(b, token) | (extra or {}))


def rows(b):
    with b.w.engine.connect() as c:
        return {t: c.scalar(text(f'SELECT count(*) FROM {t}')) for t in
            ('library_items', 'paper_versions', 'source_files', 'ingestion_jobs')}


def keys(b):
    return {k for k, _ in b.storage.orphan_keys()}


@pytest.mark.parametrize('kind', ['markdown', 'pdf'])
def test_acceptance_real_storage_private_exact_fk_new_proofs(upload, monkeypatch, caplog, kind):
    b = upload; data = b.data if kind == 'markdown' else pdf()
    token = b.make(data, 'synthetic.' + ('md' if kind == 'markdown' else 'pdf'))
    guards = []; real = auth_safety.require_final; journal = b.w.path.read_bytes(); sql=[]
    def guard(db, **kw):
        assert not db.new and not db.dirty and not db.deleted
        result=real(db, **kw); guards.append((db.get_transaction(), kw.get('writer', False)))
        return result
    monkeypatch.setattr(auth_safety, 'require_final', guard)
    def trace(c,cu,s,p,ctx,m): sql.append(s.lower())
    event.listen(get_engine(), 'before_cursor_execute', trace)
    try: r=send(b, token, data)
    finally: event.remove(get_engine(), 'before_cursor_execute', trace)
    assert r.status_code == 201, r.text
    assert set(r.json()) == {'item_id','version_id','file_id','job_id','state'} and r.json()['state']=='received'
    assert rows(b)==dict.fromkeys(rows(b),1)
    with Session(b.w.runtime) as db:
        i=db.scalar(select(UploadIntent)); s=db.scalar(select(SourceFile)); item=db.scalar(select(LibraryItem)); j=db.scalar(select(IngestionJob))
        assert i.used_at==NOW and i.item_id==item.id and i.token_hash==hash_token(token)
        assert (item.visibility,item.owner_user_id)==('private',b.w.uid)
        assert (s.item_id,s.version_id,s.job_id)==(item.id,j.version_id,j.id)
        assert j.origin=='upload' and j.source_file_id is None and j.idempotency_key==str(i.id)
        assert s.checksum_sha256==hashlib.sha256(data).hexdigest() and s.byte_size==len(data)
        assert s.page_count==(1 if kind=='pdf' else None)
        body=b.storage.open(s.storage_key)
        try: assert body.read()==data
        finally: body.close()
        dump=' '.join(db.execute(text('SELECT row_to_json(t)::text FROM audit_events t')).scalars())
    assert b.w.path.read_bytes()==journal
    for secret in (token, b.w.actor.token, b.w.actor.csrf_token, 'synthetic.md', data.decode(errors='ignore')):
        assert secret not in caplog.text+dump
    own=[g for g in guards if g[1]]
    assert len(own)==1 and all(not t.is_active for t,_ in guards)
    exclusive=next(n for n,s in enumerate(sql) if 'pg_advisory_xact_lock(' in s)
    response=next(n for n,s in enumerate(sql) if n>exclusive and 'for share' in s)
    assert not any('insert ' in s or 'update ' in s or 'for share' in s or 'for update' in s for s in sql[exclusive+1:response])
    assert send(b,token,data).status_code==404


@pytest.mark.parametrize('fault,status', [('hash',422),('size',422),('binary',422),('pdf',422),
    ('length',413),('negative',422),('type',422),('pages',422),('large',413),('probe',422)])
def test_format_rejected_no_object_or_job(upload, monkeypatch, fault, status):
    b=upload; data=b.data; extra={}
    if fault=='binary': data=b'\x00binary'
    if fault=='pdf': data=b'not a PDF'
    if fault=='pages': data=pdf(101)
    token=b.make(data, 'synthetic.pdf' if fault in ('pdf','pages','probe') else 'synthetic.md')
    if fault=='hash': data=b'x'*len(data)
    if fault=='size': data=data[:-1]
    if fault=='length': extra={'Content-Length':str(len(data)+1)}
    if fault=='negative': extra={'Content-Length':'-1'}
    if fault=='type': extra={'Content-Type':'image/png'}
    if fault=='large': monkeypatch.setenv('RAS_FILES__MARKDOWN_MAX_BYTES','4'); get_settings.cache_clear()
    if fault=='probe': monkeypatch.setattr(uploads,'page_count',lambda *a:None)
    r=send(b,token,data,extra)
    assert r.status_code==status, r.text
    assert not any(rows(b).values()) and not keys(b)


@pytest.mark.parametrize('phase', ['dependency','body','parser','put','readback','pre','post'])
@pytest.mark.parametrize('change', ['revoke','disable','generation','quarantine','held','receipt_credential',
    'receipt_status','wrong_receipt','head','latest_credential','latest_status','role','csrf','hold'])
def test_fresh_actor_through_io_and_response(upload, monkeypatch, phase, change):
    b=upload; token=b.make(); injected=[];puts=[];real_put=b.storage.put
    def track_put(*a):puts.append(True);return real_put(*a)
    monkeypatch.setattr(b.storage,'put',track_put)
    def inject():
        if not injected: alter(b,change); injected.append(True)
    if phase=='dependency': b.after_dependency=lambda *a:inject()
    elif phase=='body':
        def content(): inject(); yield b.data
    elif phase=='parser':
        real=uploads._validate_file
        def validate(*a): result=real(*a); inject(); return result
        monkeypatch.setattr(uploads,'_validate_file',validate)
    elif phase in ('put','readback'):
        name='put' if phase=='put' else 'open'; real=getattr(b.storage,name)
        def io(*a): result=real(*a); inject(); return result
        monkeypatch.setattr(b.storage,name,io)
    elif phase=='pre':
        real=original.accept
        def accept_file(*a): inject(); return real(*a)
        monkeypatch.setattr(original,'accept',accept_file)
    else:
        real=original.receipt
        def receipt(*a): inject(); return real(*a)
        monkeypatch.setattr(original,'receipt',receipt)
    r=send(b,token,content() if phase=='body' else None)
    assert injected and r.status_code==(503 if change=='hold' else 403 if change in ('role','csrf') else 401), r.text
    assert 'item_id' not in r.text
    assert all(n==(1 if phase=='post' else 0) for n in rows(b).values())
    assert len(keys(b))==(1 if phase=='post' else 0)
    if phase in ('dependency','body','parser'):assert not puts  # pre-put authority must reject BEFORE S3


@pytest.mark.parametrize('phase',['before_put','consume','response'])
@pytest.mark.parametrize('kind',['idle','absolute','intent'])
@pytest.mark.parametrize('offset',[-1,0,1])
def test_clock_after_last_sql_boundary(upload,monkeypatch,phase,kind,offset):
    b=upload; token=b.make(); end=NOW+timedelta(seconds=120 if kind=='intent' else 90)
    if kind=='idle': monkeypatch.setenv('RAS_AUTH__SESSION_IDLE','PT90S'); get_settings.cache_clear()
    if kind=='absolute':
        with b.w.engine.begin() as c: c.execute(text('UPDATE sessions SET absolute_expires_at=:e WHERE id=:s'),{'e':end,'s':b.w.actor.session_id})
    real=original.proof; proofs=[]; stage=[]
    def proof(*a,**kw): proofs.append(True); return real(*a,**kw)
    monkeypatch.setattr(original,'proof',proof)
    if phase=='consume':
        real_consume=original._consumed
        def consumed(*a): stage.append(True); return real_consume(*a)
        monkeypatch.setattr(original,'_consumed',consumed)
    if phase=='response':
        real_receipt=original.receipt
        def receipt(*a): stage.append(True); return real_receipt(*a)
        monkeypatch.setattr(original,'receipt',receipt)
    real_live=upload_intents_module().session_live; crossed=[]
    def live(*a,**kw):
        result=real_live(*a,**kw)
        if (phase=='before_put' and len(proofs)==2) or (phase!='before_put' and stage):
            b.at=end+timedelta(microseconds=offset); crossed.append(True)
        return result
    # _user uses resource_writes._actor; _check has its own imported predicate.
    from app.api import resource_writes
    monkeypatch.setattr(upload_intents_module(),'session_live',live)
    monkeypatch.setattr(resource_writes,'session_live',live)
    r=send(b,token)
    success=offset<0 or (phase=='response' and kind=='intent')
    assert crossed and r.status_code==(201 if success else 409 if kind=='intent' else 401), r.text
    persisted=success or phase=='response'
    assert all(n==int(persisted) for n in rows(b).values()) and len(keys(b))==int(persisted)


def upload_intents_module():
    from app.api import upload_intents
    return upload_intents


@pytest.mark.parametrize('mode',['partial','same_size','short','precommit','lost_reply','rollback_reply'])
def test_integrity_commit_and_cleanup_faults(upload,monkeypatch,mode):
    b=upload; token=b.make(); real_put=b.storage.put
    if mode in ('partial','same_size','short'):
        def put(key,file,mime):
            if mode in ('same_size','short'):
                import io
                real_put(key,io.BytesIO(b'x'*(len(b.data) if mode=='same_size' else 1)),mime)
            else:
                real_put(key,file,mime)
                raise OSError('synthetic put failure')
        monkeypatch.setattr(b.storage,'put',put)
    if mode=='precommit':
        real=original._consumed
        def check(*a): real(*a); raise OSError('synthetic SQL failure')
        monkeypatch.setattr(original,'_consumed',check)
    if mode in ('lost_reply','rollback_reply'):
        real=Session.commit
        def commit(db):
            if db.scalar(text("SELECT count(*) FROM source_files")):
                real(db) if mode=='lost_reply' else db.rollback()
                raise OSError('synthetic lost commit reply')
            real(db)
        monkeypatch.setattr(Session,'commit',commit)
    r=send(b,token)
    assert r.status_code==(409 if mode in ('same_size','short') else 503),r.text
    assert 'item_id' not in r.text
    assert all(n==int(mode=='lost_reply') for n in rows(b).values())
    assert len(keys(b))==int(mode in ('lost_reply','rollback_reply'))
    if mode in ('lost_reply','rollback_reply'): assert r.json()=={'error':'upload_outcome_uncertain'}


def test_two_requests_same_intent_only_one_consumes_and_loser_deletes_own_key(upload,monkeypatch):
    b=upload; token=b.make(); real=original.verify; barrier=threading.Barrier(2); staged=[]
    def verify(*a): real(*a); staged.append(a[1]); barrier.wait(15)
    monkeypatch.setattr(original,'verify',verify)
    with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda _:send(b,token),range(2)))
    assert sorted(r.status_code for r in results)==[201,404]
    assert len(set(staged))==2 and rows(b)==dict.fromkeys(rows(b),1) and len(keys(b))==1


def test_worker_advance_and_intent_expiry_before_response_keep_receipt(upload,monkeypatch):
    b=upload; token=b.make(); real=original.receipt
    from app.ingest import worker
    monkeypatch.setattr(worker,'utcnow',b.clock)
    def receipt(*a):
        accepted=a[2]
        with Session(b.w.runtime) as db:
            lease=worker.claim_job(db,accepted.job_id,get_settings(),NOW)
            worker._begin(db,accepted.job_id,lease,'validate')
            def save(db,job,item,version,source):
                item.active_version_id=version.id
                item.content_status='indexed'  # synthetic legal progression, no index/provider claim
                return 'extracting'
            worker._finish(db,accepted.job_id,lease,'validate',save)
        b.at=NOW+timedelta(minutes=3)
        return real(*a)
    monkeypatch.setattr(original,'receipt',receipt)
    r=send(b,token)
    assert r.status_code==201 and r.json()['state']=='received'
    with b.w.runtime.connect() as c:
        assert c.scalar(text('SELECT state FROM ingestion_jobs'))=='extracting'
        assert c.scalar(text('SELECT content_status FROM library_items'))=='indexed'


@pytest.mark.parametrize('phase',['body','parser','put','readback'])
def test_io_has_no_transaction_connection_or_locks_auth_can_commit(upload,monkeypatch,phase):
    b=upload; token=b.make(); started=threading.Event(); release=threading.Event()
    def pause():
        assert get_engine().pool.checkedout()==0
        started.set(); assert release.wait(15)
    if phase=='body':
        def content(): pause(); yield b.data
    else:
        obj=uploads if phase=='parser' else b.storage
        name='_validate_file' if phase=='parser' else 'put' if phase=='put' else 'open'
        real=getattr(obj,name)
        def io(*a): pause(); return real(*a)
        monkeypatch.setattr(obj,name,io)
    with ThreadPoolExecutor(2) as pool:
        future=pool.submit(send,b,token,content() if phase=='body' else None)
        try:
            assert started.wait(15)
            # Real journaled runtime revoke commits while blocked I/O owns no DB.
            pool.submit(close,b.w).result(10)
        finally: release.set()
        assert future.result(15).status_code==401
    assert not any(rows(b).values()) and not keys(b)


def test_production_pending_no_patch(installation,monkeypatch):
    from tests.helpers import DEV_ENV
    for k,v in DEV_ENV.items(): monkeypatch.setenv(k,v)
    get_settings.cache_clear()
    with TestClient(app,base_url='http://web.test') as c:
        c.cookies.set('ras_csrf','synthetic')
        r=c.put('/uploads/original',content=b'file',headers={'Origin':'http://web.test','X-CSRF-Token':'synthetic'})
        assert r.status_code==503 and r.json()=={'error':'service_recovering'}
        assert c.get('/healthz').status_code==200 and c.get('/readyz').status_code==503


@pytest.mark.parametrize('field,value',[('upload_max_concurrent',0),('upload_max_concurrent',65),
    ('upload_body_timeout_seconds',0),('upload_body_timeout_seconds',601)])
def test_upload_resource_config_bounds(field,value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): FileLimits(**{field:value})


def test_upload_resource_defaults_and_sdk_config():
    f=FileLimits()
    assert f.upload_max_concurrent==4 and f.upload_body_timeout_seconds==90
    assert f.verified_download_max_concurrent==4
    s=ObjectStorage(endpoint_url='http://127.0.0.1:1',access_key='test',secret_key='test')
    assert (s.connect_timeout_seconds,s.read_timeout_seconds,s.max_attempts)==(10,30,2)


@pytest.mark.parametrize('phase',['body','parser','put','readback','commit'])
@pytest.mark.parametrize('fault',['disconnect','cancel'])
def test_abort_thread_ownership_slot_temp_no_accept_before_commit(upload,monkeypatch,phase,fault):
    b=upload; token=b.make(); entered=threading.Event(); release=threading.Event(); paths=[]
    real_validate=uploads._validate_file
    def validate(path,*a):
        paths.append(path)
        if phase=='parser': entered.set(); assert release.wait(15)
        return real_validate(path,*a)
    if phase in ('put','readback'):
        name='put' if phase=='put' else 'open'; real=getattr(b.storage,name)
        def io(*a):
            result=real(*a)
            entered.set(); assert release.wait(15)
            return result
        monkeypatch.setattr(b.storage,name,io)
    if phase=='commit':
        real_commit=Session.commit
        def commit(db):
            if db.scalar(text('SELECT count(*) FROM source_files')):
                entered.set(); assert release.wait(15)
            return real_commit(db)
        monkeypatch.setattr(Session,'commit',commit)
    # Principal comes from the real session dependency + current predicate,
    # not a fake role/session or a dependency override that supplies authority.
    from app.api.deps import get_principal,get_session_context
    from pathlib import Path
    async def run():
        aborted=asyncio.Event(); body_sent=False
        async def receive():
            nonlocal body_sent
            if phase=='body':
                entered.set()
                await aborted.wait()
                return {'type':'http.disconnect'}
            if not body_sent:
                body_sent=True
                return {'type':'http.request','body':b.data,'more_body':False}
            if aborted.is_set(): return {'type':'http.disconnect'}
            await asyncio.Event().wait()
        h=headers(b,token)|{'Cookie':f'ras_session={b.w.actor.token}; ras_csrf={b.w.actor.csrf_token}'}
        req=Request({'type':'http','method':'PUT','path':'/uploads/original',
            'headers':[(k.lower().encode(),v.encode()) for k,v in h.items()]},receive)
        with Session(get_engine()) as db:
            ctx=get_session_context(req,db=db,now=b.clock(),settings=get_settings(),clock=b.clock)
            principal=get_principal(ctx)
        task=asyncio.create_task(original.receive(req,principal,b.clock,get_settings(),b.storage,validate))
        assert await asyncio.to_thread(entered.wait,15)
        assert original.slots.active==1
        if fault=='cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert original.slots.active==1  # parent gone, thread/receive still owned
        aborted.set()
        if paths: assert all(Path(p).exists() for p in paths)
        if phase=='commit': assert len(keys(b))==1
        release.set()
        if fault=='disconnect':
            try:
                result=await task
                assert phase=='commit' and result['state']=='received'
            except ApiError:
                assert phase!='commit'
        while original._attempts:
            await asyncio.gather(*list(original._attempts),return_exceptions=True)
        assert original.slots.active==0 and all(not Path(p).exists() for p in paths)
    asyncio.run(run())
    assert all(n==int(phase=='commit') for n in rows(b).values())
    assert len(keys(b))==int(phase=='commit')


@pytest.mark.parametrize('fault',['temp','deadline','slot'])
def test_receive_limits_and_temp_failure_release_resources(upload,monkeypatch,fault):
    b=upload; token=b.make()
    if fault=='temp':
        def fail(*a,**kw): raise OSError('synthetic tempfile failure')
        monkeypatch.setattr(original.tempfile,'TemporaryDirectory',fail)
        assert send(b,token).status_code==503
    elif fault=='slot':
        original.slots.acquire(1)
        monkeypatch.setenv('RAS_FILES__UPLOAD_MAX_CONCURRENT','1');get_settings.cache_clear()
        try: assert send(b,token).status_code==503
        finally: original.slots.release()
    else:
        monkeypatch.setenv('RAS_FILES__UPLOAD_BODY_TIMEOUT_SECONDS','0.05');get_settings.cache_clear()
        from app.api.deps import get_principal,get_session_context
        async def run():
            async def receive(): await asyncio.Event().wait()
            h=headers(b,token)|{'Cookie':f'ras_session={b.w.actor.token}'}
            req=Request({'type':'http','method':'PUT','path':'/uploads/original',
                'headers':[(k.lower().encode(),v.encode()) for k,v in h.items()]},receive)
            with Session(get_engine()) as db:
                principal=get_principal(get_session_context(req,db=db,now=b.clock(),settings=get_settings(),clock=b.clock))
            with pytest.raises(ApiError) as exc:
                await original.receive(req,principal,b.clock,get_settings(),b.storage,uploads._validate_file)
            assert exc.value.code=='upload_body_timeout'
        asyncio.run(run())
    assert not any(rows(b).values()) and not keys(b)


@pytest.mark.parametrize('mode',['missing','foreign','session','admin','expired','notice','origin','duplicate_origin','csrf'])
def test_intent_acl_and_denials_before_body_storage(upload,monkeypatch,mode):
    b=upload; token=b.make(); h={}; expect=404
    if mode=='missing': token='missing'
    if mode=='foreign':
        # A second User is created by runtime invitation acceptance, then login.
        original_uid,original_actor=b.w.uid,b.w.actor
        b.w.uid,b.w.actor=b.admin_id,b.admin_actor
        accept(b.w,issue(b.w,email='second@example.test'))
        with Session(b.w.runtime) as db:
            actor=login(db,email='second@example.test',password=PASSWORD,now=NOW,settings=get_settings(),clock=b.clock)[1]
        b.w.uid,b.w.actor=original_uid,actor
        b.client.cookies.set('ras_session',actor.token);b.client.cookies.set('ras_csrf',actor.csrf_token)
    if mode=='session':
        with Session(b.w.runtime) as db:
            actor=login(db,email=EMAIL,password=PASSWORD,now=NOW,settings=get_settings(),clock=b.clock)[1]
        b.w.actor=actor;b.client.cookies.set('ras_session',actor.token);b.client.cookies.set('ras_csrf',actor.csrf_token)
    if mode=='admin':
        b.w.actor=b.admin_actor;b.client.cookies.set('ras_session',b.admin_actor.token);b.client.cookies.set('ras_csrf',b.admin_actor.csrf_token)
    if mode=='expired': b.at=NOW+timedelta(minutes=2)
    if mode=='notice': monkeypatch.setenv('RAS_UPLOAD_NOTICE_VERSION','changed');get_settings.cache_clear()
    if mode=='origin':h={'Origin':'http://evil.test'};expect=403
    if mode=='csrf':h={'X-CSRF-Token':'wrong'};expect=403
    if mode=='duplicate_origin':
        r=b.client.put('/uploads/original',content=b.data,headers=list(headers(b,token).items())+[('Origin','http://web.test')])
        assert r.status_code==403
    else: assert send(b,token,extra=h).status_code==expect
    assert not any(rows(b).values()) and not keys(b)


@pytest.mark.parametrize('field',['filename','declared_size','declared_sha256','expires_at','notice_version','ack'])
@pytest.mark.parametrize('phase',['parser','readback','response'])
def test_exact_metadata_and_consent_rechecked(upload,monkeypatch,field,phase):
    b=upload; token=b.make()
    def change():
        statements={
            'filename':"UPDATE upload_intents SET filename='different.md'",
            'declared_size':'UPDATE upload_intents SET declared_size=declared_size+1',
            'declared_sha256':"UPDATE upload_intents SET declared_sha256=repeat('f',64)",
            'expires_at':"UPDATE upload_intents SET expires_at=expires_at+interval '1 second'",
            'notice_version':"UPDATE upload_intents SET notice_version='old'",
            'ack':"UPDATE processing_acknowledgements SET notice_version='old'",
        }
        with b.w.engine.begin() as c:c.execute(text(statements[field]))
    obj=uploads if phase=='parser' else b.storage if phase=='readback' else original
    name='_validate_file' if phase=='parser' else 'open' if phase=='readback' else 'receipt'
    real=getattr(obj,name)
    def call(*a):
        if phase=='response':change();return real(*a)
        result=real(*a);change();return result
    monkeypatch.setattr(obj,name,call)
    r=send(b,token)
    assert r.status_code==409 and 'item_id' not in r.text
    assert all(n==int(phase=='response') for n in rows(b).values()) and len(keys(b))==int(phase=='response')


def test_cleanup_db_unavailable_keeps_orphan_and_reconcile_after_settled_grace(upload,monkeypatch):
    b=upload;token=b.make();real_put=b.storage.put;real_engine=original.get_engine
    def put(*a):real_put(*a);raise OSError('synthetic partial put')
    monkeypatch.setattr(b.storage,'put',put)
    real_cleanup=original.cleanup
    def cleanup(storage,key):
        def failed():raise OSError('synthetic DB unreachable')
        monkeypatch.setattr(original,'get_engine',failed)
        try:real_cleanup(storage,key)
        finally:monkeypatch.setattr(original,'get_engine',real_engine)
    monkeypatch.setattr(original,'cleanup',cleanup)
    assert send(b,token).status_code==503 and len(keys(b))==1
    monkeypatch.setattr(b.storage,'put',real_put)
    assert send(b,token).status_code==201 and len(keys(b))==2
    from app.cli import reconcile_originals
    from app.clock import utcnow
    monkeypatch.setattr(reconcile_originals,'get_storage',lambda settings:b.storage)
    monkeypatch.setattr(reconcile_originals,'utcnow',lambda:utcnow()+timedelta(days=2))
    reconcile_originals.main()
    assert len(keys(b))==1 and rows(b)==dict.fromkeys(rows(b),1)


@pytest.mark.parametrize('wait',['user','intent','journal','response_item'])
@pytest.mark.parametrize('kind',['idle','intent'])
def test_clock_after_observed_final_lock_wait(upload,monkeypatch,wait,kind):
    b=upload;token=b.make();entered=threading.Event();release=threading.Event()
    monkeypatch.setenv('RAS_RECOVERY__GATE_LOCK_TIMEOUT_MS','10000')
    monkeypatch.setenv('RAS_AUTH__SESSION_IDLE','PT90S');get_settings.cache_clear()
    real_gate=auth_safety.require_final;real_accept=original.accept;real_receipt=original.receipt
    def pause():entered.set();assert release.wait(15)
    if wait=='journal':
        def gate(db,**kw):
            if kw.get('writer'):pause()
            return real_gate(db,**kw)
        monkeypatch.setattr(auth_safety,'require_final',gate)
    elif wait=='response_item':
        def receipt(*a):b.accepted=a[2];pause();return real_receipt(*a)
        monkeypatch.setattr(original,'receipt',receipt)
    else:
        def accept_file(*a):pause();return real_accept(*a)
        monkeypatch.setattr(original,'accept',accept_file)
    with ThreadPoolExecutor(1) as pool:
        f=pool.submit(send,b,token)
        assert entered.wait(15)
        with Session(b.w.runtime) as blocker:
            if wait=='user':
                from app.identity.predicates import lock_users
                lock_users(blocker,[b.w.uid])
            elif wait=='intent':
                blocker.execute(select(UploadIntent.id).where(UploadIntent.token_hash==hash_token(token)).with_for_update()).all()
            elif wait=='response_item':
                blocker.execute(select(LibraryItem.id).where(LibraryItem.id==b.accepted.item_id).with_for_update()).all()
            else:
                from app import recovery
                blocker.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':recovery.JOURNAL_LOCK})
            release.set()
            wait_for(b.w,'advisory' if wait=='journal' else 'transactionid')
            b.at=NOW+timedelta(seconds=120 if kind=='intent' else 90)
            # For intent expiry, keep caller live to isolate the intent clock.
            if kind=='intent':
                with b.w.engine.begin() as c:
                    # privileged timestamp fixture, no auth rights granted
                    c.execute(text('UPDATE sessions SET last_activity_at=:a WHERE id=:s'),{'a':b.at,'s':b.w.actor.session_id})
            blocker.rollback()
        r=f.result(15)
    success=wait=='response_item' and kind=='intent'
    assert r.status_code==(201 if success else 409 if kind=='intent' else 401),r.text
    persisted=wait=='response_item'
    assert all(n==int(persisted) for n in rows(b).values()) and len(keys(b))==int(persisted)


def test_different_intents_and_owners_same_bytes_independent_private(upload):
    b=upload
    assert send(b,b.make()).status_code==201
    assert send(b,b.make()).status_code==201
    a=b.w.uid;b.w.uid,b.w.actor=b.admin_id,b.admin_actor
    user=accept(b.w,issue(b.w,email='independent@example.test'))
    with Session(b.w.runtime) as db:
        actor=login(db,email='independent@example.test',password=PASSWORD,now=NOW,settings=get_settings(),clock=b.clock)[1]
    b.w.uid,b.w.actor=user.id,actor
    b.client.cookies.set('ras_session',actor.token);b.client.cookies.set('ras_csrf',actor.csrf_token)
    assert send(b,b.make()).status_code==201
    with b.w.runtime.connect() as c:
        assert c.scalar(text('SELECT count(DISTINCT storage_key) FROM source_files'))==3
        assert c.scalar(text('SELECT count(*) FROM library_items WHERE visibility=\'private\' AND owner_user_id=:u'),{'u':a})==2
        assert c.scalar(text('SELECT count(*) FROM library_items WHERE visibility=\'private\' AND owner_user_id=:u'),{'u':user.id})==1


def test_chunked_no_content_length_checks_incremental_size(upload):
    b=upload;token=b.make()
    def content():yield b.data[:4];yield b.data[4:]
    assert send(b,token,content()).status_code==201
    token=b.make()
    def overflow():yield b.data;yield b'one byte too many'
    assert send(b,token,overflow()).status_code==413
    assert rows(b)==dict.fromkeys(rows(b),1) and len(keys(b))==1


@pytest.mark.parametrize('field,value',[('connect_timeout_seconds',0),('read_timeout_seconds',121),('max_attempts',0),('max_attempts',6)])
def test_storage_timeout_bounds(field,value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):ObjectStorage(endpoint_url='http://127.0.0.1',access_key='test',secret_key='test',**{field:value})


def test_storage_sdk_timeouts_are_configured(upload):
    c=upload.storage.client.meta.config
    assert c.connect_timeout==10 and c.read_timeout==30
    assert c.retries=={'mode':'standard','total_max_attempts':2}


def test_real_http_through_dedicated_streaming_proxy(upload, tmp_path):
    """Live uvicorn + actual TS proxy handler; HTTP smoke, not a browser DOM."""
    import json,socket,subprocess,time
    from pathlib import Path
    import httpx,uvicorn
    b=upload
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:threading.Event().wait(.01)
    assert server.started
    repo=Path(__file__).resolve().parents[2]
    route=(repo/'web/app/api/uploads/original/route.ts').as_uri()
    proxy=(repo/'web/lib/proxy.ts').as_uri()
    code=f'''
import {{createServer}} from 'node:http';
import {{Readable}} from 'node:stream';
import {{PUT}} from {json.dumps(route)};
import {{proxyRequest,loadProxyConfig}} from {json.dumps(proxy)};
process.env.API_INTERNAL_URL='http://127.0.0.1:{port}';
const server=createServer(async(req,res)=>{{
  try{{
    const request=new Request('http://web.test'+req.url,{{method:req.method,headers:req.headers,
      ...(req.method==='GET'?{{}}:{{body:Readable.toWeb(req),duplex:'half'}})}});
    const result=req.method==='PUT'?await PUT(request):await proxyRequest(request,req.url.slice(5).split('/'),loadProxyConfig(process.env));
    res.writeHead(result.status,{{'content-type':result.headers.get('content-type'),'set-cookie':result.headers.getSetCookie()}});
    res.end(Buffer.from(await result.arrayBuffer()));
  }}catch{{res.writeHead(502);res.end('{{"error":"upload_outcome_uncertain"}}')}}
}});
server.listen(0,'127.0.0.1',()=>console.log(server.address().port));
'''
    process=None
    try:
        process=subprocess.Popen(['node','--input-type=module','-e',code],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        proxy_port=int(process.stdout.readline().strip())
        with httpx.Client(base_url=f'http://127.0.0.1:{proxy_port}',timeout=20) as client:
            client.cookies.set('ras_session',b.w.actor.token);client.cookies.set('ras_csrf',b.w.actor.csrf_token)
            data=b.data; h={'Origin':'http://web.test','X-CSRF-Token':b.w.actor.csrf_token}
            intent=client.post('/api/uploads/intents',headers=h,json=b.body|{'byte_size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            assert intent.status_code==201
            token=intent.json()['intent_token']
            def content():yield data[:3];yield data[3:]
            r=client.put('/api/uploads/original',headers=h|{'X-Upload-Intent':token,'Content-Type':'application/octet-stream'},content=content())
            assert r.status_code==201 and r.json()['state']=='received'
            item=r.json()['item_id']
            detail=client.get('/api/library/items/'+item)
            assert detail.status_code==200 and detail.json()['item']['owned_by_me']
            assert client.put('/api/uploads/original',headers=h|{'X-Upload-Intent':token,'Content-Type':'application/octet-stream'},content=data).status_code==404
            close(b.w)
            denied=client.put('/api/uploads/original',headers=h|{'X-Upload-Intent':token,'Content-Type':'application/octet-stream'},content=data)
            assert denied.status_code==401 and any('Max-Age=0' in c for c in denied.headers.get_list('set-cookie'))
    finally:
        if process:
            process.terminate()
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.kill();process.wait(10)
            process.stdout.close()
        server.should_exit=True;thread.join(10)
        assert not thread.is_alive()
    assert rows(b)==dict.fromkeys(rows(b),1)


def test_pdf_probe_timeout_rejects_before_storage(upload,monkeypatch):
    from app import pdf_probe
    import subprocess
    b=upload;data=pdf();token=b.make(data,'synthetic.pdf')
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('synthetic probe',get_settings().files.pdf_probe_timeout_seconds)
    monkeypatch.setattr(pdf_probe.subprocess,'run',timeout)
    assert send(b,token,data).status_code==422 and not any(rows(b).values()) and not keys(b)


@pytest.mark.parametrize('phase',['parser','put','readback','consume','response'])
def test_configured_notice_reload_during_upload_is_rejected(upload,monkeypatch,phase):
    b=upload;token=b.make();puts=[];real_put=b.storage.put
    def track(*a):puts.append(True);return real_put(*a)
    monkeypatch.setattr(b.storage,'put',track)
    def reload():
        monkeypatch.setenv('RAS_UPLOAD_NOTICE_VERSION','reloaded.v2');get_settings.cache_clear()
    obj=uploads if phase=='parser' else b.storage if phase in ('put','readback') else original
    name='_validate_file' if phase=='parser' else 'put' if phase=='put' else 'open' if phase=='readback' else '_consumed' if phase=='consume' else 'receipt'
    real=getattr(obj,name)
    def call(*a):
        if phase in ('consume','response'):reload();return real(*a)
        result=real(*a);reload();return result
    monkeypatch.setattr(obj,name,call)
    r=send(b,token)
    assert r.status_code==409 and r.json()=={'error':'notice_changed'},r.text
    assert all(n==int(phase=='response') for n in rows(b).values()) and len(keys(b))==int(phase=='response')
    if phase=='parser':assert not puts
