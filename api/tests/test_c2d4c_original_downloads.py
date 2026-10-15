"""Runtime PostgreSQL/MinIO/journal original GET boundaries and ownership.

Shared business setup uses the real C4 contract. Auth accounts use bootstrap/invite/
login, resources use privileged setup, corruption/faults never alter grants.
"""
import asyncio
import hashlib
import io
import os
import time
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

from app import auth_safety, common_original, publication, promotion, recovery, journal
from app.api import original_downloads as boundary
from app.api.errors import ApiError
from app.api.routes import uploads, reviews
from app.config import get_settings
from app.db import get_engine
from app.identity import final_caller as callers
from app.identity.login import login
from app.models import ReviewRequest, SourceFile, UserSession
from app.models.enums import AuditOutcome, RevocationReason, RightsAudience
from app.storage import get_storage
from tests.resource_fixtures import source, common_copy, runtime_approve, BODY, SHA
from tests.test_auth_safety_foundation import NOW
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as world, issue, accept, PASSWORD, EMAIL
from tests.test_c2d1_final_caller import browser, mutate, wait_for
from tests.test_c2d2b_review_publication_reads import request as review_request
from tests.test_review_requests import close_journaled, journal_sql_close
from tests.test_uploads import pdf

BRANCHES = ('private', 'common', 'review')


@pytest.fixture
def downloads(browser, monkeypatch):
    b = browser
    assert all(os.environ.get(k) for k in ('MINIO_ROOT_USER', 'MINIO_ROOT_PASSWORD', 'MINIO_API_HOST_PORT'))
    monkeypatch.setenv('RAS_STORAGE__BUCKET', 'ras-c2d4c-' + uuid.uuid4().hex)
    get_settings.cache_clear()
    b.storage = get_storage(get_settings()); b.storage.ensure_bucket()
    b.own = source(b.w, b.w.uid, 'private synthetic')
    b.user = accept(b.w, issue(b.w))
    b.review = review_request(b.w, b.user.id)
    b.common = common_copy(b.w, download=True, indexed=False)
    for ids in (b.own, b.review, b.common):
        b.storage.put(ids.key, io.BytesIO(BODY), 'text/markdown')
    b.ids = {'private': b.own, 'common': b.common, 'review': b.review}
    b.path = lambda branch: (f'/admin/review-requests/{b.review.request}/files/{b.review.file}/original'
        if branch == 'review' else f'/files/{b.ids[branch].file}/original')
    app = __import__('app.main', fromlist=['app']).app
    app.dependency_overrides[uploads._storage] = lambda: b.storage
    app.dependency_overrides[reviews._storage] = lambda: b.storage
    b.spools = []
    real = common_original.VerifiedOriginal.open_spool
    def opened(owner):
        value = real(owner); b.spools.append(value); return value
    monkeypatch.setattr(common_original.VerifiedOriginal, 'open_spool', opened)
    yield b
    assert not boundary._attempts
    assert all(s.closed for s in b.spools)
    assert get_engine().pool.checkedout() == 0
    for key, _ in b.storage.orphan_keys(''): b.storage.delete(key)
    b.storage.client.delete_bucket(Bucket=b.storage.bucket)


def audits(b):
    with b.w.engine.connect() as c:
        return c.execute(text("SELECT action,outcome,target_type,target_id,details FROM audit_events "
            "WHERE action IN ('common_copy.original.download','review.original.view') ORDER BY id")).all()


def denied(r, status, code=None):
    assert r.status_code == status, r.text
    if code: assert r.json() == {'error': code}
    assert 'content-disposition' not in r.headers
    assert BODY not in r.content
    if status == 401: assert 'Max-Age=0' in r.headers['set-cookie']


@pytest.mark.parametrize('branch', BRANCHES)
@pytest.mark.parametrize('kind', ('markdown', 'pdf'))
def test_three_branches_size_hash_headers_audit_new_transactions(downloads, monkeypatch, branch, kind, caplog):
    b = downloads; ids=b.ids[branch]; data = BODY if kind=='markdown' else pdf()
    if kind=='pdf':
        digest=hashlib.sha256(data).hexdigest()
        with b.w.engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            c.execute(text("UPDATE source_files SET checksum_sha256=:h,byte_size=:n,verified_mime='application/pdf',"
                "original_filename=:name WHERE id=:f"), {'h':digest,'n':len(data),'f':ids.file,'name':'quote"\r\n.pdf'})
            if branch=='review': c.execute(text('UPDATE review_requests SET file_checksum_sha256=:h WHERE id=:r'),{'h':digest,'r':ids.request})
            if branch=='common':
                c.execute(text('UPDATE access_rights SET file_checksum_sha256=:h WHERE item_id=:i'),{'h':digest,'i':ids.item})
                c.execute(text('UPDATE common_publications SET file_checksum_sha256=:h,verified_byte_size=:n WHERE item_id=:i'),{'h':digest,'n':len(data),'i':ids.item})
        b.storage.put(ids.key,io.BytesIO(data),'application/pdf')
    phases=[]; real_proof=boundary.proof; real_audit=boundary.audit; real_open=b.storage.open
    def proof(*a,**kw):
        result=real_proof(*a,**kw); phases.append('response' if len(a)>5 else 'initial');return result
    def audit(*a,**kw):
        result=real_audit(*a,**kw);phases.append('committed');return result
    def opened(key):
        assert get_engine().pool.checkedout()==0
        phases.append('storage');return real_open(key)
    monkeypatch.setattr(boundary,'proof',proof);monkeypatch.setattr(boundary,'audit',audit);monkeypatch.setattr(b.storage,'open',opened)
    journal=b.w.path.read_bytes();before=audits(b)
    r=b.client.get(b.path(branch))
    assert r.status_code==200,r.text
    assert r.content==data and hashlib.sha256(r.content).hexdigest()==hashlib.sha256(data).hexdigest()
    assert phases==(['initial','storage','response'] if branch=='private' else ['initial','storage','committed','response'])
    assert r.headers['content-length']==str(len(data)) and r.headers['cache-control']=='no-store'
    assert r.headers['x-content-type-options']=='nosniff'
    assert '\r' not in r.headers['content-disposition'] and '\n' not in r.headers['content-disposition']
    assert b.w.path.read_bytes()==journal
    assert len(audits(b))-len(before)==int(branch!='private')
    for secret in (ids.key,SHA,b.w.actor.token, 'quote"',BODY.decode()): assert secret not in caplog.text+str(audits(b))


@pytest.mark.parametrize('branch', BRANCHES)
def test_unpatched_pending_no_storage_no_business_audit(installation, monkeypatch, branch):
    from app.main import app
    def forbidden(*a): raise AssertionError('pending reached storage')
    monkeypatch.setattr(boundary,'_prepare',forbidden)
    path=(f'/admin/review-requests/{uuid.uuid4()}/files/{uuid.uuid4()}/original' if branch=='review'
          else f'/files/{uuid.uuid4()}/original')
    with TestClient(app) as client:
        denied(client.get(path),503,'service_recovering')
        assert client.get('/healthz').status_code==200 and client.get('/readyz').status_code==503
    with installation.runtime.connect() as c: assert c.scalar(text('SELECT count(*) FROM audit_events'))==0


def test_a_b_admin_exact_files_same_bytes_no_ordinary_review_grant(downloads):
    b=downloads
    other=accept(b.w,issue(b.w,email='other@example.test'))
    ids_b=source(b.w,other.id,'B same bytes');b.storage.put(ids_b.key,io.BytesIO(BODY),'text/markdown')
    tokens=[]
    for email in (EMAIL,'other@example.test'):
        with Session(b.w.runtime) as db: tokens.append(login(db,email=email,password=PASSWORD,now=NOW,settings=b.w.settings,clock=b.clock)[1].token)
    from app.main import app
    for token,own,foreign in ((tokens[0],b.review,ids_b),(tokens[1],ids_b,b.review)):
        with TestClient(app,base_url='http://web.test') as client:
            client.cookies.set('ras_session',token)
            assert client.get(f'/files/{own.file}/original').content==BODY
            denied(client.get(f'/files/{foreign.file}/original'),404,'not_found')
            assert client.get(b.path('common')).content==BODY
            denied(client.get(b.path('review')),403,'forbidden')
    denied(b.client.get(f'/files/{b.review.file}/original'),404,'not_found')
    assert b.client.get(b.path('review')).content==BODY
    for path in (f'/files/{uuid.uuid4()}/original','/files/not-id/original',
        f'/admin/review-requests/{b.review.request}/files/{b.own.file}/original',
        f'/admin/review-requests/{uuid.uuid4()}/files/{b.review.file}/original'):
        denied(b.client.get(path),404,'not_found')


@pytest.mark.parametrize('change',('pending','epoch','rights_revoked','publication_revoked','permit','audience','conditions','checksum','size','wrong_file','wrong_version','no_rights'))
def test_common_exact_gate_before_storage(downloads,monkeypatch,change):
    b=downloads
    sql={'pending':"UPDATE library_items SET publication_state='pending' WHERE id=:i",'epoch':'UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i',
        'rights_revoked':"UPDATE access_rights SET revoked_at=:at,revoked_by_user_id=:u,revocation_reason_code='rights_withdrawn',rights_epoch_at_revoke=1 WHERE item_id=:i",'publication_revoked':'UPDATE common_publications SET revoked_at=:at WHERE item_id=:i',
        'permit':'UPDATE access_rights SET permits_original_download=false WHERE item_id=:i',"audience":"UPDATE access_rights SET scope_audience='other' WHERE item_id=:i",
        'conditions':"UPDATE access_rights SET scope_conditions='unimplemented' WHERE item_id=:i",'checksum':"UPDATE common_publications SET file_checksum_sha256=:h WHERE item_id=:i",
        'size':'UPDATE common_publications SET verified_byte_size=1 WHERE item_id=:i','wrong_file':'UPDATE access_rights SET source_file_id=gen_random_uuid() WHERE item_id=:i',
        'wrong_version':'UPDATE access_rights SET version_id=gen_random_uuid() WHERE item_id=:i','no_rights':'DELETE FROM access_rights WHERE item_id=:i'}[change]
    with b.w.engine.begin() as c:
        if change=='audience':
            # The schema allows only instance_users; this isolated corruption
            # verifies the SQL gate too, without widening any runtime grants.
            c.execute(text('ALTER TABLE access_rights DROP CONSTRAINT ck_access_rights_scope_audience_valid'))
        c.execute(text("SET LOCAL session_replication_role='replica'"));c.execute(text(sql),{'i':b.common.item,'at':NOW,'h':'0'*64,'u':b.w.uid})
    monkeypatch.setattr(b.storage,'open',lambda key:pytest.fail('denied gate opened object'))
    denied(b.client.get(b.path('common')),404,'not_found')


@pytest.mark.parametrize('change',('withdrawn','rejected','closed_by_recovery','epoch','checksum','other_file','other_version','owner'))
def test_review_exact_gate_before_storage(downloads,monkeypatch,change):
    b=downloads
    if change in ('withdrawn','rejected','closed_by_recovery'):
        close_review(b, change)
    else:
        sql={'epoch':'UPDATE library_items SET rights_epoch=1 WHERE id=:i','checksum':"UPDATE review_requests SET file_checksum_sha256=:h WHERE id=:r",
            'other_file':'UPDATE review_requests SET source_file_id=:f WHERE id=:r','other_version':'UPDATE review_requests SET version_id=:v WHERE id=:r',
            'owner':'UPDATE library_items SET owner_user_id=:u WHERE id=:i'}[change]
        with b.w.engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"));c.execute(text(sql),{'i':b.review.item,'r':b.review.request,'h':'0'*64,'f':b.own.file,'v':b.own.version,'u':b.w.uid})
    monkeypatch.setattr(b.storage,'open',lambda key:pytest.fail('inactive review opened object'))
    denied(b.client.get(b.path('review')),404,'not_found')


@pytest.mark.parametrize('branch',BRANCHES)
@pytest.mark.parametrize('fault',('same_size','short','long','missing','open','read','close','temp','write','flush','seek','short_write','spool_corrupt'))
def test_integrity_io_faults_no_bytes_original_retained_slot_reusable(downloads,monkeypatch,branch,fault):
    b=downloads;ids=b.ids[branch];real_open=b.storage.open
    monkeypatch = pytest.MonkeyPatch()  # fault-only patches; leave fixture env intact
    if fault in ('same_size','short','long'):
        b.storage.put(ids.key,io.BytesIO(b'x'*len(BODY) if fault=='same_size' else BODY[:-1] if fault=='short' else BODY+b'x'),'text/markdown')
    elif fault=='missing':b.storage.delete(ids.key)
    elif fault=='temp':monkeypatch.setattr(common_original.tempfile,'TemporaryFile',lambda **kw:(_ for _ in ()).throw(OSError('synthetic temp')))
    elif fault in ('write','flush','seek','short_write','spool_corrupt'):
        real=common_original.VerifiedOriginal.open_spool
        class FailWrite:
            def __init__(self,spool):self.spool=spool
            def __getattr__(self,name):return getattr(self.spool,name)
            def write(self,data):
                if fault=='write':raise OSError('synthetic write')
                return self.spool.write(data[:-1] if fault=='short_write' else b'x'*len(data) if fault=='spool_corrupt' else data)
            def flush(self):
                if fault=='flush':raise OSError('synthetic flush')
                return self.spool.flush()
            def seek(self,*a):
                if fault=='seek':raise OSError('synthetic seek')
                return self.spool.seek(*a)
            def close(self):self.spool.close()
        def opened(v):s=real(v);return FailWrite(s)
        monkeypatch.setattr(common_original.VerifiedOriginal,'open_spool',opened)
    else:
        class FaultBody:
            def __init__(self,body):self.body=body
            def iter_chunks(self,**kw):
                for chunk in self.body.iter_chunks(**kw):
                    if fault=='read':raise OSError('synthetic read')
                    yield chunk
            def close(self):
                self.body.close()
                if fault=='close':raise OSError('synthetic close')
        def opened(key):
            if fault=='open':raise OSError('synthetic open')
            return FaultBody(real_open(key))
        monkeypatch.setattr(b.storage,'open',opened)
    integrity=fault in ('same_size','short','long','short_write','spool_corrupt') or (fault=='missing' and branch!='review')
    denied(b.client.get(b.path(branch)),409 if integrity else 503,branch+'_file_integrity_failed' if integrity else 'file_unavailable')
    assert ids.key in {k for k,_ in b.storage.orphan_keys('')} or fault=='missing'
    monkeypatch.undo()  # fixture monkeypatches are needed below: restore explicit storage/cutover
    from app.main import app
    app.dependency_overrides[uploads._storage]=lambda:b.storage;app.dependency_overrides[reviews._storage]=lambda:b.storage
    b.storage.put(ids.key,io.BytesIO(BODY),'text/markdown')
    assert b.client.get(b.path(branch)).content==BODY


AUTH_CHANGES=('revoke','disable','generation','quarantine','held','receipt_credential','receipt_status','wrong_receipt','head','latest_credential','latest_status','hold','ahead','unconfirmed')
@pytest.mark.parametrize('branch',BRANCHES)
@pytest.mark.parametrize('phase',('dependency','storage','response'))
@pytest.mark.parametrize('change',AUTH_CHANGES)
def test_current_caller_real_heads_receipts_binding_after_slow_work(downloads,monkeypatch,branch,phase,change):
    b=downloads;real_open=b.storage.open;real_proof=boundary.proof
    if phase=='dependency':b.after_dependency=lambda db,ctx:mutate(b,change)
    if phase=='storage':
        def opened(key):
            assert get_engine().pool.checkedout()==0;mutate(b,change);return real_open(key)
        monkeypatch.setattr(b.storage,'open',opened)
    if phase=='response':
        def proof(*a,**kw):
            if len(a)>5:mutate(b,change)
            return real_proof(*a,**kw)
        monkeypatch.setattr(boundary,'proof',proof)
    expected=503 if change in ('hold','ahead','unconfirmed','receipt_credential','receipt_status','wrong_receipt','head','latest_credential','latest_status') else 401
    denied(b.client.get(b.path(branch)),expected,'service_recovering' if expected==503 else 'session_expired')


@pytest.mark.parametrize('branch',BRANCHES)
@pytest.mark.parametrize('phase',('storage','postcommit','final_sql'))
def test_current_role_review_only_owner_role_change_allowed(downloads,monkeypatch,branch,phase):
    b=downloads;real_open=b.storage.open;real_audit=boundary.audit;real_target=boundary._target
    if phase=='storage':
        def opened(key):mutate(b,'role');return real_open(key)
        monkeypatch.setattr(b.storage,'open',opened)
    elif phase=='postcommit':
        def audit(*a):real_audit(*a);mutate(b,'role')
        monkeypatch.setattr(boundary,'audit',audit)
        if branch=='private':
            def opened(key):mutate(b,'role');return real_open(key)
            monkeypatch.setattr(b.storage,'open',opened)
    else:
        count=0
        def target(*a):
            nonlocal count
            value=real_target(*a);count+=1
            if count==(4 if branch=='private' else 6):mutate(b,'role')
            return value
        monkeypatch.setattr(boundary,'_target',target)
    r=b.client.get(b.path(branch))
    if branch=='review':denied(r,403,'forbidden')
    else:assert r.status_code==200 and r.content==BODY,r.text


@pytest.mark.parametrize('branch',BRANCHES)
@pytest.mark.parametrize('kind',('idle','absolute'))
@pytest.mark.parametrize('sql',('gate','predicate'))
@pytest.mark.parametrize('offset',(-1,0))
def test_clock_after_final_gate_and_predicate_sql(downloads,monkeypatch,branch,kind,sql,offset):
    b=downloads;end=NOW+timedelta(seconds=1800 if kind=='idle' else 240)
    if kind=='absolute':
        with b.w.engine.begin() as c:c.execute(text('UPDATE sessions SET absolute_expires_at=:at WHERE id=:s'),{'at':end,'s':b.w.actor.session_id})
    armed=False;calls=0;real_proof=boundary.proof;real_target=boundary._target;real_live=callers.session_live
    def proof(*a,**kw):
        nonlocal armed
        armed=len(a)>5
        return real_proof(*a,**kw)
    def target(*a):
        nonlocal calls
        value=real_target(*a)
        if armed:
            calls+=1
            if calls==2 and sql=='gate':b.at=end+timedelta(seconds=offset)
        return value
    def live(*a,**kw):
        nonlocal calls
        value=real_live(*a,**kw)
        if armed and sql=='predicate':
            calls+=1
            if calls==4:b.at=end+timedelta(seconds=offset)
        return value
    monkeypatch.setattr(boundary,'proof',proof);monkeypatch.setattr(boundary,'_target',target);monkeypatch.setattr(callers,'session_live',live)
    r=b.client.get(b.path(branch))
    if offset==0:denied(r,401,'session_expired')
    else:assert r.status_code==200 and r.content==BODY,r.text
    assert b.at==end+timedelta(seconds=offset)
    with b.w.engine.connect() as c:assert c.scalar(text('SELECT last_activity_at FROM sessions WHERE id=:s'),{'s':b.w.actor.session_id})==NOW


@pytest.mark.parametrize('branch',('common','review'))
@pytest.mark.parametrize('outcome',('precommit','rollback_reply','landed_uncertain','known_commit'))
@pytest.mark.parametrize('teardown',('none','close','rollback_close'))
def test_audit_outcome_independent_teardown_no_retry_bytes_or_delete(downloads,monkeypatch,branch,outcome,teardown):
    b=downloads;real_commit=Session.commit;real_close=Session.close;real_rollback=Session.rollback
    states=[];calls=0;active=set();real_session=boundary._session
    def owned(function,**kw):
        state=kw.get('outcome')
        if state is None:return real_session(function,**kw)
        states.append(state)
        def wrapped(db):active.add(db);return function(db)
        return real_session(wrapped,**kw)
    def commit(db):
        nonlocal calls
        if db in active:
            calls+=1
            if outcome=='precommit':raise OSError('precommit synthetic')
            if outcome=='rollback_reply':real_rollback(db);raise OSError('rollback reply synthetic')
            real_commit(db)
            if outcome=='landed_uncertain':raise OSError('landed reply synthetic')
        else:real_commit(db)
    def close(db):
        if db in active and teardown!='none':raise OSError('close synthetic')
        real_close(db)
    def rollback(db):
        if db in active and teardown=='rollback_close':raise OSError('rollback synthetic')
        real_rollback(db)
    monkeypatch.setattr(boundary,'_session',owned);monkeypatch.setattr(Session,'commit',commit);monkeypatch.setattr(Session,'close',close);monkeypatch.setattr(Session,'rollback',rollback)
    r=b.client.get(b.path(branch))
    if outcome=='known_commit' and teardown=='none':assert r.status_code==200 and r.content==BODY
    else:denied(r,503,'file_unavailable' if outcome=='known_commit' else 'download_audit_outcome_uncertain')
    assert calls==1 and len(states)==1
    assert states[0].commit_started and states[0].committed==(outcome=='known_commit')
    assert len(audits(b))==int(outcome in ('landed_uncertain','known_commit'))
    assert b.ids[branch].key in {k for k,_ in b.storage.orphan_keys('')}


def close_review(b, ending):
    if ending == 'approved':
        # C2d-5b runtime approval: real proofs/copy/journal on the isolated MinIO.
        runtime_approve(b.w,b.review.request,b.storage,title='synthetic approval',source='synthetic author',
            evidence='synthetic permission',download=True,search=False)
    elif ending != 'closed_by_recovery':
        close_journaled(b.w.engine,b.review.request,how='withdraw' if ending=='withdrawn' else 'reject')
    else:
        # Durable rejected intent + rolled-back closure, then real restricted
        # replay. Journal status is the original decision, never recovery status.
        with Session(b.w.engine) as db:
            rr=db.scalar(select(ReviewRequest).where(ReviewRequest.id==b.review.request).with_for_update())
            body={'request_id':str(rr.id),'item_id':str(rr.item_id),'version_id':str(rr.version_id),
                'source_file_id':str(rr.source_file_id),'status':'rejected','reason_code':'other','closed_at':NOW.isoformat()}
            recovery._lock_journal(db)
            state=journal.read(b.w.path)
            journal.append(b.w.path,state,[journal.next_record(state.head,journal.REVIEW_CLOSURE,body)])
            db.rollback()
        recovery.recover(b.w.recovery,SimpleNamespace(),b.w.settings,NOW)
        with b.w.engine.connect() as c:
            assert c.scalar(text('SELECT status FROM review_requests WHERE id=:r'),{'r':b.review.request})=='closed_by_recovery'


def end_grant(b,branch,ending='withdrawn'):
    if branch=='review':close_review(b,ending)
    else:
        from tests.resource_fixtures import runtime_revoke
        runtime_revoke(b.w,b.common.item,clock=b.clock)


@pytest.mark.parametrize('phase',('storage','postcommit','after_boundary'))
@pytest.mark.parametrize('branch,ending',[('common','revoked')]+[('review',v) for v in ('withdrawn','rejected','approved','closed_by_recovery')])
def test_closure_revoke_before_or_after_response_boundary(downloads,monkeypatch,branch,phase,ending):
    b=downloads;real_open=b.storage.open;real_audit=boundary.audit;real_proof=boundary.proof
    if phase=='storage':
        ended=False
        def opened(key):
            nonlocal ended
            if not ended:
                ended=True;end_grant(b,branch,ending)
            return real_open(key)
        monkeypatch.setattr(b.storage,'open',opened)
    elif phase=='postcommit':
        def audit(*a):real_audit(*a);end_grant(b,branch,ending)
        monkeypatch.setattr(boundary,'audit',audit)
    else:
        def proof(*a):
            value=real_proof(*a)
            if len(a)>5:end_grant(b,branch,ending)
            return value
        monkeypatch.setattr(boundary,'proof',proof)
    r=b.client.get(b.path(branch))
    if phase=='after_boundary':assert r.status_code==200 and r.content==BODY
    else:denied(r,404,'not_found')
    denied(b.client.get(b.path(branch)),404,'not_found')
    assert len([a for a in audits(b) if a.outcome=='success'])==int(phase!='storage')


@pytest.mark.parametrize('branch',BRANCHES)
def test_blocked_storage_releases_connection_locks_and_holds_shared_pool(downloads,monkeypatch,branch):
    b=downloads;monkeypatch.setenv('RAS_FILES__VERIFIED_DOWNLOAD_MAX_CONCURRENT','1');get_settings.cache_clear()
    entered=threading.Event();release=threading.Event();real_open=b.storage.open
    def opened(key):
        assert get_engine().pool.checkedout()==0
        entered.set();assert release.wait(15);return real_open(key)
    monkeypatch.setattr(b.storage,'open',opened)
    with ThreadPoolExecutor(2) as pool:
        future=pool.submit(b.client.get,b.path(branch));assert entered.wait(10)
        for other in BRANCHES:denied(b.client.get(b.path(other)),503,'download_busy')
        try:
            if branch=='private':mutate(b,'revoke')
            else:end_grant(b,branch)
        finally:release.set()
        denied(future.result(10),401 if branch=='private' else 404)
    assert all(s.closed for s in b.spools)


@pytest.mark.parametrize('branch',('common','review'))
def test_audit_resource_locks_queue_writer_response_proof_sees_winner(downloads,monkeypatch,branch):
    b=downloads;real=boundary._audit;writer=None
    if branch=='common':
        # A separate invited User downloads while the Admin revokes. The
        # resource row, rather than their shared User row, must queue the writer.
        from tests.test_c2c2_invitations import EMAIL, PASSWORD
        with Session(b.w.runtime) as db:
            actor=login(db,email=EMAIL,password=PASSWORD,now=NOW,settings=b.w.settings,clock=b.clock)[1]
        b.client.cookies.set('ras_session',actor.token)
    with ThreadPoolExecutor(1) as pool:
        def audit(db,p,target,outcome,**kw):
            nonlocal writer
            value=real(db,p,target,outcome,**kw)
            if outcome==AuditOutcome.SUCCESS:
                writer=pool.submit(end_grant,b,branch)
                wait_for_resource_writer(b.w,branch)
            return value
        monkeypatch.setattr(boundary,'_audit',audit)
        r=b.client.get(b.path(branch));writer.result(10)
        denied(r,404,'not_found')


def wait_for_resource_writer(w,branch):
    deadline=time.monotonic()+10
    with w.engine.connect() as observer:
        while time.monotonic()<deadline:
            waiting=observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                "AND wait_event='transactionid' AND usename=:name AND query LIKE :table)"),
                {'name':w.runtime.url.username if branch=='common' else w.engine.url.username,
                 'table':'%library_items%' if branch=='common' else '%review_requests%'})
            observer.rollback()
            if waiting:return
            threading.Event().wait(.005)
    raise AssertionError('isolated resource writer did not wait on download row lock')


@pytest.mark.parametrize('branch',BRANCHES)
@pytest.mark.parametrize('cancel',('task','disconnect'))
@pytest.mark.parametrize('phase',('storage','allocated'))
def test_http_cancel_running_executor_retains_slot_temp_until_settled(downloads,monkeypatch,branch,cancel,phase):
    b=downloads;monkeypatch.setenv('RAS_FILES__VERIFIED_DOWNLOAD_MAX_CONCURRENT','1');get_settings.cache_clear()
    entered=threading.Event();release=threading.Event();real_open=b.storage.open
    def opened(key):entered.set();assert release.wait(15);return real_open(key)
    if phase=='storage':monkeypatch.setattr(b.storage,'open',opened)
    else:
        real_prepare=boundary._prepare
        def prepare(*a):
            value=real_prepare(*a);entered.set();assert release.wait(15);return value
        monkeypatch.setattr(boundary,'_prepare',prepare)
    async def run():
        disconnected=asyncio.Event()
        async def receive():await disconnected.wait();return {'type':'http.disconnect'}
        request=Request({'type':'http','method':'GET','headers':[]},receive)
        p=boundary.Principal(b.w.uid,'admin',b.w.actor.session_id,'admin@example.test',None)
        task=asyncio.create_task(boundary.receive(request,p,str(b.ids[branch].file),b.clock,get_settings(),b.storage,
            str(b.review.request) if branch=='review' else None))
        assert await asyncio.to_thread(entered.wait,10)
        if cancel=='task':task.cancel()
        else:disconnected.set()
        with pytest.raises(asyncio.CancelledError if cancel=='task' else ClientDisconnect):await task
        assert len(boundary._attempts)==1 and b.spools[-1].closed is False
        slots=common_original._slots(1);assert not slots.acquire(blocking=False)
        release.set()
        settled_results=await asyncio.gather(*list(boundary._attempts),return_exceptions=True)
        await asyncio.sleep(0)
        assert settled_results and b.spools[-1].closed and not boundary._attempts
        assert slots.acquire(blocking=False);slots.release()
    try:asyncio.run(run())
    finally:release.set()
    assert b.client.get(b.path(branch)).content==BODY


@pytest.mark.parametrize('spec',('2.3','2.4'))
@pytest.mark.parametrize('leave',('headers','body','disconnect_before','disconnect_after','complete','unused'))
def test_response_owns_slot_temp_all_asgi_paths(spec,leave):
    slots=threading.BoundedSemaphore(1);assert slots.acquire(blocking=False)
    v=common_original.VerifiedOriginal(slots);spool=v.open_spool();spool.write(b'x'*131072)
    response=common_original.VerifiedFileResponse(v,headers={'content-length':'131072'})
    async def run():
        gone=asyncio.Event();first=asyncio.Event();messages=[]
        async def receive():await gone.wait();return {'type':'http.disconnect'}
        async def send(message):
            if leave=='headers' and message['type']=='http.response.start':raise OSError('synthetic header send')
            if leave=='disconnect_before' and message['type']=='http.response.start':gone.set();await asyncio.Future()
            if message['type']=='http.response.body' and message.get('body'):
                first.set()
                if leave=='body':raise OSError('synthetic body send')
                if leave=='disconnect_after':gone.set();await asyncio.Future()
            messages.append(message)
        if leave=='unused':response._verified.close();return
        if spec=='2.4' and leave.startswith('disconnect'):
            async def failing(message):
                if leave=='disconnect_before' or message['type']=='http.response.body':raise OSError('disconnect')
            sender=failing
        else:sender=send
        try:await response({'type':'http','asgi':{'spec_version':spec}},receive,sender)
        except (OSError,ClientDisconnect,ExceptionGroup):pass
        if leave=='complete':assert sum(len(m.get('body',b'')) for m in messages)==131072
    asyncio.run(run())
    assert v.closed and spool.closed and slots.acquire(blocking=False)
    slots.release()


@pytest.mark.parametrize('branch',BRANCHES)
def test_sql_order_transactions_and_every_storage_temp_operation_outside_db(downloads,monkeypatch,branch):
    b=downloads;sql=[];transactions=[];real_session=boundary._session
    def owned(function,**kw):
        def wrapped(db):
            info=db.connection().info
            info['c2d4c_phase']='audit' if kw.get('outcome') is not None else 'proof'
            try:return function(db)
            finally:info.pop('c2d4c_phase',None)
        return real_session(wrapped,**kw)
    def trace(c,cu,s,p,ctx,m):
        phase=c.info.get('c2d4c_phase')
        if phase:
            sql.append((phase,s.lower(),c.get_transaction()))
            if c.get_transaction() not in transactions:transactions.append(c.get_transaction())
    real_spool=common_original.VerifiedOriginal.open_spool;real_open=b.storage.open
    class CheckedSpool:
        def __init__(self,spool):self.spool=spool
        def __getattr__(self,name):
            value=getattr(self.spool,name)
            if not callable(value):return value
            def operation(*a,**kw):assert get_engine().pool.checkedout()==0;return value(*a,**kw)
            return operation
    def spool(v):
        assert get_engine().pool.checkedout()==0
        value=CheckedSpool(real_spool(v));v._hold.spool=value;return value
    class CheckedBody:
        def __init__(self,body):self.body=body
        def iter_chunks(self,**kw):
            for block in self.body.iter_chunks(**kw):assert get_engine().pool.checkedout()==0;yield block
        def close(self):assert get_engine().pool.checkedout()==0;self.body.close()
    def opened(key):assert get_engine().pool.checkedout()==0;return CheckedBody(real_open(key))
    monkeypatch.setattr(boundary,'_session',owned);monkeypatch.setattr(common_original.VerifiedOriginal,'open_spool',spool);monkeypatch.setattr(b.storage,'open',opened)
    event.listen(get_engine(),'before_cursor_execute',trace)
    try:r=b.client.get(b.path(branch))
    finally:event.remove(get_engine(),'before_cursor_execute',trace)
    assert r.status_code==200 and r.content==BODY,r.text
    assert len(transactions)==(2 if branch=='private' else 3) and all(not t.is_active for t in transactions)
    for txn in transactions:
        statements=[s for _,s,t in sql if t is txn]
        exclusive=[n for n,s in enumerate(statements) if 'pg_advisory_xact_lock(' in s]
        shared=[n for n,s in enumerate(statements) if 'pg_advisory_xact_lock_shared(' in s]
        locks=[n for n,s in enumerate(statements) if 'for share' in s]
        assert locks
        if exclusive:
            assert not shared and len(exclusive)==1
            assert any(s.lstrip().startswith('insert into audit_events') for s in statements[:exclusive[0]])
            boundary_at=exclusive[0]
        else:
            assert shared;boundary_at=shared[0]
        assert max(locks)<boundary_at
        assert not any('for share' in s or 'for update' in s or s.lstrip().startswith(('insert ','update ','delete ')) for s in statements[boundary_at+1:])


@pytest.mark.parametrize('branch',BRANCHES)
@pytest.mark.parametrize('phase',('dependency','storage','response'))
def test_exact_session_user_binding_never_accepts_another_live_user(downloads,monkeypatch,branch,phase):
    b=downloads;real_open=b.storage.open;real_proof=boundary.proof
    def rebind():
        with b.w.engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            c.execute(text('UPDATE sessions SET user_id=:u WHERE id=:s'),{'u':b.user.id,'s':b.w.actor.session_id})
    if phase=='dependency':b.after_dependency=lambda db,ctx:rebind()
    elif phase=='storage':
        def opened(key):rebind();return real_open(key)
        monkeypatch.setattr(b.storage,'open',opened)
    else:
        def proof(*a):
            if len(a)>5:rebind()
            return real_proof(*a)
        monkeypatch.setattr(boundary,'proof',proof)
    denied(b.client.get(b.path(branch)),401,'session_expired')


@pytest.mark.parametrize('branch',('common','review'))
@pytest.mark.parametrize('kind',('idle','absolute'))
@pytest.mark.parametrize('offset',(-1,0))
def test_expiry_during_known_audit_commit_uses_new_response_proof(downloads,monkeypatch,branch,kind,offset):
    b=downloads;monkeypatch.setenv('RAS_AUTH__SESSION_IDLE','PT7M');get_settings.cache_clear()
    end=NOW+timedelta(seconds=420 if kind=='idle' else 240)
    if kind=='absolute':
        with b.w.engine.begin() as c:c.execute(text('UPDATE sessions SET absolute_expires_at=:e WHERE id=:s'),{'e':end,'s':b.w.actor.session_id})
    real_audit=boundary.audit
    def audit(*a):real_audit(*a);b.at=end+timedelta(seconds=offset)
    monkeypatch.setattr(boundary,'audit',audit)
    r=b.client.get(b.path(branch))
    if offset==0:denied(r,401,'session_expired')
    else:assert r.status_code==200 and r.content==BODY,r.text
    assert len(audits(b))==1 and audits(b)[0].outcome=='success'


def test_temp_close_failure_tries_raw_cleanup_and_releases_slot():
    slots=threading.BoundedSemaphore(1);assert slots.acquire(blocking=False)
    v=common_original.VerifiedOriginal(slots);spool=v.open_spool()
    class BrokenClose:
        raw=spool.raw
        def close(self):raise OSError('synthetic temp close')
    v._hold.spool=BrokenClose()
    with pytest.raises(OSError):v.close()
    assert v.closed and spool.closed and slots.acquire(blocking=False)
    slots.release()


@pytest.mark.parametrize('spec',('2.3','2.4'))
def test_cancel_stream_with_temp_read_thread_running_retains_owner(spec):
    slots=threading.BoundedSemaphore(1);assert slots.acquire(blocking=False)
    v=common_original.VerifiedOriginal(slots);spool=v.open_spool();spool.write(b'x'*131072)
    entered=threading.Event();release=threading.Event()
    class BlockingRead:
        def __getattr__(self,name):return getattr(spool,name)
        def read(self,*a):entered.set();assert release.wait(10);return spool.read(*a)
    v._hold.spool=BlockingRead()
    response=common_original.VerifiedFileResponse(v)
    async def run():
        messages=[];gone=False
        async def receive():await asyncio.Future()
        async def send(message):
            # ASGI 2.3 may attempt a chunk as its shielded worker settles;
            # a server drops sends to the already disconnected client.
            if not gone:messages.append(message)
        task=asyncio.create_task(response({'type':'http','asgi':{'spec_version':spec}},receive,send))
        assert await asyncio.to_thread(entered.wait,5)
        gone=True;task.cancel();await asyncio.sleep(.05)
        assert not v.closed and not spool.closed and not slots.acquire(blocking=False)
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert not any(m.get('body') for m in messages)
    try:asyncio.run(run())
    finally:release.set()
    assert v.closed and spool.closed and slots.acquire(blocking=False)
    slots.release()


@pytest.mark.parametrize('branch',BRANCHES)
def test_malformed_denial_rechecks_revoked_caller_without_spool(downloads,branch):
    b=downloads;b.after_dependency=lambda db,ctx:mutate(b,'revoke')
    path=f'/admin/review-requests/{b.review.request}/files/not-id/original' if branch=='review' else '/files/not-id/original'
    denied(b.client.get(path),401,'session_expired')
    assert not b.spools and not audits(b)


@pytest.mark.parametrize('branch',('common','review'))
@pytest.mark.parametrize('landed',(False,True))
@pytest.mark.parametrize('close_session',(False,True))
def test_audit_uncertain_and_session_temp_close_errors_preserve_primary_outcome(downloads,monkeypatch,branch,landed,close_session):
    b=downloads;active=set();real_session=boundary._session;real_commit=Session.commit;real_close=Session.close
    real_spool=common_original.VerifiedOriginal.open_spool
    def owned(function,**kw):
        def write(db):active.add(db);return function(db)
        return real_session(write if kw.get('outcome') is not None else function,**kw)
    def commit(db):
        if db not in active:return real_commit(db)
        if landed:real_commit(db)
        else:db.rollback()
        raise OSError('synthetic uncertain commit')
    def close(db):
        if db in active and close_session:raise OSError('synthetic session close')
        real_close(db)
    class BrokenClose:
        def __init__(self,spool):self.spool=spool
        def __getattr__(self,name):return getattr(self.spool,name)
        def close(self):raise OSError('synthetic spool close')
    def spool(v):
        value=BrokenClose(real_spool(v));v._hold.spool=value;return value
    monkeypatch.setattr(boundary,'_session',owned);monkeypatch.setattr(Session,'commit',commit);monkeypatch.setattr(Session,'close',close)
    monkeypatch.setattr(common_original.VerifiedOriginal,'open_spool',spool)
    denied(b.client.get(b.path(branch)),503,'download_audit_outcome_uncertain')
    assert len(audits(b))==int(landed)
    assert b.ids[branch].key in {k for k,_ in b.storage.orphan_keys('')}
