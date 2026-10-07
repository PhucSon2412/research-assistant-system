"""Runtime review writers, exact SQL mirrors, final clocks and commit outcomes.

Accounts/session/head/receipt come from real identity. Only business cutover
is scoped; resource corruption and restore setup use a separate test owner.
"""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery
from app.api import review_writes as boundary, resource_writes
from app.api.errors import ApiError
from app.api.routes import reviews
from app.authz import Principal
from app.config import get_settings
from app.db import get_engine
from app.identity.login import login
from app.identity.sessions import revoke_session
from app.identity.session_commit import commit_auth
from app.models import ReviewRequest
from app.models.enums import SessionRevokeReason
from tests.resource_fixtures import source, SHA, MemoryOriginals
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import world, issue, accept, PASSWORD, EMAIL
from tests.test_c2d1_final_caller import browser, wait_for
from tests.test_auth_safety_foundation import NOW

ACTIONS = ('submit', 'withdraw', 'reject')


@pytest.fixture
def writers(browser):
    b = browser
    b.user = accept(b.w, issue(b.w))
    with Session(b.w.runtime) as db:
        b.owner_session = login(db, email=EMAIL, password=PASSWORD, now=NOW,
            settings=get_settings(), clock=b.clock)[1]
    b.own = source(b.w, b.user.id, 'PRIVATE-SYNTHETIC-TITLE')
    b.other = source(b.w, b.w.uid, 'OTHER-IDENTICAL-BYTES')
    b.owner = Principal(b.user.id, 'user', b.owner_session.session_id, EMAIL, None)
    b.admin = Principal(b.w.uid, 'admin', b.w.actor.session_id, 'admin@example.test', None)
    b.body = reviews.ReviewRequestInput(acknowledged=True, notice_version=get_settings().review.notice_version,
        item_id=b.own.item, version_id=b.own.version, source_file_id=b.own.file,
        declared_source='PRIVATE-SOURCE-DECLARATION', declared_rights='PRIVATE-RIGHTS-DECLARATION')
    b.rid = None
    from app.main import app
    app.dependency_overrides[reviews._storage] = lambda: MemoryOriginals(b.own)
    return b


def request(b, action, *, who=None, body=None, rid=None):
    who = who or ('admin' if action == 'reject' else 'owner')
    issued = b.w.actor if who == 'admin' else b.owner_session
    b.client.cookies.set('ras_session', issued.token)
    b.client.cookies.set('ras_csrf', issued.csrf_token)
    data = b.body.model_dump(mode='json') if action == 'submit' else {'reason_code':'other','note':'PRIVATE-NOTE'} if action == 'reject' else None
    path = '/review-requests' if action == 'submit' else (
        f'/admin/review-requests/{rid or b.rid}/reject' if action == 'reject' else f'/review-requests/{rid or b.rid}/withdraw')
    return b.client.post(path, json=body if body is not None else data,
        headers={'Origin':'http://web.test','X-CSRF-Token':issued.csrf_token})


def opened(b):
    r = request(b, 'submit')
    assert r.status_code == 201, r.text
    b.rid = uuid.UUID(r.json()['request']['id'])
    return b.rid


def rows(b):
    with b.w.engine.connect() as c:
        return c.execute(text('SELECT id,status,closed_by_user_id,rejection_note FROM review_requests ORDER BY id')).all()


def audit(b):
    with b.w.engine.connect() as c:
        return c.execute(text("SELECT action,target_id,details FROM audit_events WHERE action LIKE 'review.request.%' ORDER BY id")).all()


def direct(b, action, db, outcome):
    p = b.admin if action == 'reject' else b.owner
    csrf = b.w.actor.csrf_token if action == 'reject' else b.owner_session.csrf_token
    return boundary.write(db, p, action=action, body=b.body if action=='submit' else reviews.RejectInput(reason_code='other'),
        request_id=str(b.rid), csrf_token=csrf, clock=b.clock, settings=get_settings(), outcome=outcome)


@pytest.mark.parametrize('action', ACTIONS)
def test_runtime_success_fresh_response_lock_order_and_audit(writers, monkeypatch, action, caplog):
    b=writers
    if action!='submit': opened(b)
    before=journal.read(b.w.path)
    statements=[]; proofs=[]; writer_tx=[]
    def sql(c, cursor, statement, params, ctx, many): statements.append(statement)
    def gate(db,*a,**kw):
        writer_tx.append(db.get_transaction()); return real_gate(db,*a,**kw)
    def response(db,*a,**kw):
        assert not db.in_transaction()
        result=real_response(db,*a,**kw); proofs.append(db.get_transaction()); return result
    real_gate=boundary._gate;real_response=boundary._response
    monkeypatch.setattr(boundary,'_gate',gate);monkeypatch.setattr(boundary,'_response',response)
    event.listen(get_engine(),'before_cursor_execute',sql)
    try: r=request(b,action)
    finally:event.remove(get_engine(),'before_cursor_execute',sql)
    assert r.status_code==(201 if action=='submit' else 200),r.text
    assert proofs and all(p is not writer_tx[0] for p in proofs)
    result=r.json()['request']
    assert result['status']==('open' if action=='submit' else 'withdrawn' if action=='withdraw' else 'rejected')
    if action=='reject':assert not {'item','file','version','declaration','requester_email','original_url','rejection_note'}&result.keys()
    else:assert result['declaration']['verified'] is False
    if action!='submit':assert 'đang chờ duyệt' not in result['pending_note']
    new=journal.read(b.w.path)
    assert len(new.records)==len(before.records)+(action!='submit')
    if action!='submit':
        assert new.head.body['request_id']==str(b.rid)
        assert new.head.body['source_file_id']==str(b.own.file)
        assert set(new.head.body)==set(journal.CLOSURE_FIELDS)
    assert all(marker not in str(audit(b))+b.w.path.read_text()+caplog.text for marker in
        ('PRIVATE-SOURCE-DECLARATION','PRIVATE-RIGHTS-DECLARATION','PRIVATE-NOTE','PRIVATE-SYNTHETIC-TITLE',PASSWORD))
    start=next(i for i,s in enumerate(statements) if 'ras_auth_lock_users' in s)
    final=next(i for i,s in enumerate(statements[start:],start) if 'pg_advisory_xact_lock(' in s)
    end=next((i for i,s in enumerate(statements[final+1:],final+1) if 'FOR SHARE' in s),len(statements))
    assert any('INSERT INTO audit_events' in s for s in statements[start:final])
    assert not any('FOR UPDATE' in s or 'FOR NO KEY UPDATE' in s for s in statements[final:end])
    assert get_engine().pool.checkedout()==0


@pytest.mark.parametrize('field', ('item_id','version_id','source_file_id'))
def test_exact_tuple_identical_bytes_foreign_404(writers,field):
    b=writers; other={'item_id':b.other.item,'version_id':b.other.version,'source_file_id':b.other.file}
    body=b.body.model_dump(mode='json')|{field:str(other[field])}
    r=request(b,'submit',body=body)
    assert (r.status_code,r.json())==(404,{'error':'not_found'})
    assert not rows(b) and not audit(b)


@pytest.mark.parametrize('action',ACTIONS)
@pytest.mark.parametrize('case',('role','revoke','held','quarantine','binding','generation','receipt','head','csrf'))
def test_current_actor_after_dependency_no_business_change(writers,action,case):
    b=writers
    if action!='submit':opened(b)
    before=rows(b);before_audit=audit(b)
    def after(db,ctx):
        if case=='revoke':
            with Session(b.w.runtime) as d:
                revoke_session(d,session_id=ctx.session_id,user_id=ctx.user_id,reason=SessionRevokeReason.USER_REVOKED,now=NOW)
                commit_auth(d)
            return
        with b.w.engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            if case=='role':c.execute(text("UPDATE users SET role=:r WHERE id=:u"),{'u':ctx.user_id,'r':'user' if action=='reject' else 'admin'})
            elif case=='held':c.execute(text("UPDATE users SET status_safety='held' WHERE id=:u"),{'u':ctx.user_id})
            elif case=='quarantine':c.execute(text("UPDATE users SET credential_state='quarantined' WHERE id=:u"),{'u':ctx.user_id})
            elif case=='binding':c.execute(text('UPDATE sessions SET user_id=:u WHERE id=:s'),{'u':b.w.uid if ctx.user_id!=b.w.uid else b.user.id,'s':ctx.session_id})
            elif case=='generation':c.execute(text('UPDATE sessions SET credential_generation=:g WHERE id=:s'),{'g':uuid.uuid4(),'s':ctx.session_id})
            elif case=='receipt':c.execute(text("DELETE FROM auth_change_receipts WHERE user_id=:u AND domain='credential'"),{'u':ctx.user_id})
            elif case=='head':c.execute(text('UPDATE auth_safety_heads SET credential_generation=:g WHERE user_id=:u'),{'g':uuid.uuid4(),'u':ctx.user_id})
            elif case=='csrf':c.execute(text("UPDATE sessions SET csrf_token_hash=:h WHERE id=:s"),{'s':ctx.session_id,'h':'0'*64})
    b.after_dependency=after
    r=request(b,action)
    expected=200 if action=='withdraw' and case=='role' else 403 if case in ('role','csrf') else 401
    assert r.status_code==expected,r.text
    if expected!=200:
        assert rows(b)==before and audit(b)==before_audit
        if expected==401: assert 'Max-Age=0' in r.headers['set-cookie']


@pytest.mark.parametrize('action',ACTIONS)
@pytest.mark.parametrize('kind',('idle','absolute'))
@pytest.mark.parametrize('offset',(-1,0,1))
def test_clock_after_resource_and_predicate_sql(writers,monkeypatch,action,kind,offset):
    b=writers
    if action!='submit':opened(b)
    before=rows(b);before_audit=audit(b);final=[False]
    if kind=='absolute':
        def after(db,ctx):
            with b.w.engine.begin() as c:
                c.execute(text('UPDATE sessions SET last_activity_at=:at WHERE id=:s'),
                    {'s':ctx.session_id,'at':NOW+get_settings().auth.session_absolute-timedelta(minutes=1)})
        b.after_dependency=after
    real_gate=boundary._gate;real_live=resource_writes.session_live
    def gate(*a,**kw):
        out=real_gate(*a,**kw);final[0]=True;return out
    def live(*a,**kw):
        out=real_live(*a,**kw)
        if final[0]:b.at=NOW+(get_settings().auth.session_idle if kind=='idle' else get_settings().auth.session_absolute)+timedelta(microseconds=offset)
        return out
    monkeypatch.setattr(boundary,'_gate',gate);monkeypatch.setattr(resource_writes,'session_live',live)
    r=request(b,action)
    assert r.status_code==(201 if action=='submit' else 200) if offset<0 else r.status_code==401,r.text
    if offset>=0:assert rows(b)==before and audit(b)==before_audit


@pytest.mark.parametrize('action',('withdraw','reject'))
def test_inactive_grant_workflow_and_closed_missing_foreign(writers,action):
    b=writers;opened(b)
    with b.w.engine.begin() as c:c.execute(text('UPDATE library_items SET rights_epoch=rights_epoch+1 WHERE id=:i'),{'i':b.own.item})
    b.client.cookies.set('ras_session',b.w.actor.token)
    old=f'/admin/review-requests/{b.rid}/files/{b.own.file}/original'
    assert b.client.get(old).status_code==404
    r=request(b,action);assert r.status_code==200,r.text
    assert request(b,action).json()=={'error':'review_request_closed'}
    assert request(b,action,rid='malformed').status_code==404
    assert request(b,action,rid=str(uuid.uuid4())).status_code==404
    if action=='withdraw':assert request(b,action,who='admin').status_code==404


@pytest.mark.parametrize('override',({'acknowledged':False},{'acknowledged':None},{'acknowledged':1},{'acknowledged':'true'},{'unexpected':'secret'},
    {'declared_source_url':'file:///secret'},{'declared_license_url':'https://x.test/ bad'}))
def test_strict_input_no_ack_or_extra_fields(writers,override):
    b=writers;r=request(b,'submit',body=b.body.model_dump(mode='json')|override)
    assert r.status_code==422 and not rows(b)


def test_notice_duplicate_and_owner_role(writers):
    b=writers;body=b.body.model_dump(mode='json')|{'notice_version':'stale'}
    assert request(b,'submit',body=body).json()=={'error':'notice_changed'}
    opened(b);assert request(b,'submit').json()=={'error':'review_request_open'}
    assert request(b,'submit',who='admin').status_code==403


@pytest.mark.parametrize('action',('withdraw','reject'))
@pytest.mark.parametrize('fault',('none','torn','full_fsync'))
def test_append_fault_commits_false_mirror_and_recover(writers,monkeypatch,action,fault):
    b=writers;opened(b);real=journal.append
    def broken(path,state,records):
        if fault=='torn':
            with open(path,'ab') as f:f.write(records[0].line.encode()[:30])
        elif fault=='full_fsync':real(path,state,records)
        raise OSError('injected append/fsync')
    monkeypatch.setattr(journal,'append',broken)
    r=request(b,action);assert (r.status_code,r.json())==(503,{'error':'review_journal_unavailable'})
    assert rows(b)[0][1]==('withdrawn' if action=='withdraw' else 'rejected')
    with b.w.engine.connect() as c:assert c.scalar(text('SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed'))==1
    assert not recovery.evaluate_integrity(get_settings(),b.w.runtime).open
    monkeypatch.setattr(journal,'append',real)
    report=recovery.recover(b.w.engine,SimpleNamespace(),get_settings(),NOW,confirm_db_current=fault!='full_fsync')
    assert report.gate=='ready' and recovery.evaluate_integrity(get_settings(),b.w.runtime).open


@pytest.mark.parametrize('action',ACTIONS)
@pytest.mark.parametrize('landed',(False,True))
@pytest.mark.parametrize('teardown',(False,True))
def test_commit_exception_never_success_or_retry_even_teardown(writers,monkeypatch,action,landed,teardown):
    b=writers
    if action!='submit':opened(b)
    outcome=boundary.Outcome();db=Session(b.w.runtime,expire_on_commit=False)
    real_commit=db.commit;real_rollback=db.rollback;real_close=db.close;calls=[]
    def commit():
        calls.append(1)
        (real_commit if landed else real_rollback)()
        raise RuntimeError('lost commit reply')
    monkeypatch.setattr(db,'commit',commit)
    if teardown:
        def fail():raise RuntimeError('teardown')
        monkeypatch.setattr(db,'rollback',fail);monkeypatch.setattr(db,'close',fail)
    try:
        with pytest.raises(ApiError) as e:direct(b,action,db,outcome)
        assert e.value.code=='review_write_outcome_uncertain'
        assert outcome.commit_started and not outcome.committed and len(calls)==1
        assert outcome.teardown_failed==teardown
        if action!='submit':assert outcome.record is not None and outcome.written
    finally:real_rollback();real_close()
    assert get_engine().pool.checkedout()==0
    if action!='submit' and not landed:
        assert rows(b)[0][1]=='open'
        assert recovery.evaluate_integrity(get_settings(),b.w.runtime).reason=='db_behind_journal'
        recovery.recover(b.w.engine,SimpleNamespace(),get_settings(),NOW)
        assert rows(b)[0][1]=='closed_by_recovery'


@pytest.mark.parametrize('action',ACTIONS)
@pytest.mark.parametrize('fault',('response','rollback','close','both'))
def test_known_commit_preserved_on_response_or_teardown_failure(writers,monkeypatch,action,fault):
    b=writers
    if action!='submit':opened(b)
    outcome=boundary.Outcome();db=Session(b.w.runtime,expire_on_commit=False)
    real_rollback=db.rollback;real_close=db.close
    def fail(*a,**kw):raise RuntimeError('injected postcommit')
    if fault=='response':monkeypatch.setattr(boundary,'_response',fail)
    if fault in ('rollback','both'):monkeypatch.setattr(db,'rollback',fail)
    if fault in ('close','both'):monkeypatch.setattr(db,'close',fail)
    try:
        with pytest.raises(ApiError) as e:direct(b,action,db,outcome)
        assert e.value.code=='review_write_response_unavailable'
        assert outcome.commit_started and outcome.committed
    finally:real_rollback();real_close()
    assert rows(b)[0][1]==('open' if action=='submit' else 'withdrawn' if action=='withdraw' else 'rejected')
    assert get_engine().pool.checkedout()==0


@pytest.mark.parametrize('action',('withdraw','reject'))
def test_expiry_during_fsync_durable_intent_not_business_success(writers,monkeypatch,action):
    b=writers;opened(b);real=journal.append
    def append(*a,**kw):
        real(*a,**kw);b.at=NOW+get_settings().auth.session_idle
    monkeypatch.setattr(journal,'append',append)
    r=request(b,action);assert r.status_code==401,r.text
    assert rows(b)[0][1]=='open'
    assert recovery.evaluate_integrity(get_settings(),b.w.runtime).reason=='db_behind_journal'
    monkeypatch.setattr(journal,'append',real)
    recovery.recover(b.w.engine,SimpleNamespace(),get_settings(),b.at)
    assert rows(b)[0][1]=='closed_by_recovery'


@pytest.mark.parametrize('action',ACTIONS)
def test_known_commit_auth_loss_during_new_response_proof(writers,monkeypatch,action):
    b=writers
    if action!='submit':opened(b)
    real=boundary._response
    def response(*a,**kw):
        b.at=NOW+get_settings().auth.session_idle;return real(*a,**kw)
    monkeypatch.setattr(boundary,'_response',response)
    r=request(b,action);assert r.status_code==401,r.text
    assert rows(b)[0][1]==('open' if action=='submit' else 'withdrawn' if action=='withdraw' else 'rejected')


@pytest.mark.parametrize('action',ACTIONS)
def test_pending_http_without_cutover_patch_no_changes(installation,action):
    w=installation;before=w.path.read_bytes()
    path='/review-requests' if action=='submit' else f'/review-requests/{uuid.uuid4()}/withdraw' if action=='withdraw' else f'/admin/review-requests/{uuid.uuid4()}/reject'
    with TestClient(__import__('app.main',fromlist=['app']).app) as c:
        assert c.get('/healthz').status_code==200 and c.get('/readyz').status_code==503
        r=c.post(path,json={},headers={'Origin':'http://web.test'})
        assert r.status_code==503
    assert w.path.read_bytes()==before


def staged(b,db):
    rr=db.scalar(select(ReviewRequest).where(ReviewRequest.id==b.rid).with_for_update())
    rr.status='withdrawn';rr.closed_at=NOW;rr.closed_by_user_id=b.user.id;rr.close_reason_code='withdrawn_by_requester'
    db.flush()
    return boundary.Closure(rr.id,rr.item_id,rr.version_id,rr.source_file_id,rr.requester_user_id,b.user.id,'withdrawn','withdrawn_by_requester',NOW)


@pytest.mark.parametrize('field',journal.CLOSURE_FIELDS)
@pytest.mark.parametrize('shape',('missing','null','number','mismatch'))
def test_runtime_sql_independent_shape_binding_before_append(writers,field,shape):
    b=writers;opened(b);before=b.w.path.read_bytes();before_rows=rows(b)
    with Session(b.w.runtime) as db:
        c=staged(b,db);body=c.body()
        if shape=='missing':del body[field]
        elif shape=='null':body[field]=None
        elif shape=='number':body[field]=17
        else:body[field]=str(uuid.uuid4()) if field.endswith('_id') else 'other' if field=='reason_code' else 'rejected' if field=='status' else (NOW+timedelta(seconds=1)).isoformat()
        line=journal.canonical({'body':body,'kind':'review_closure','seq':journal.read(b.w.path).head.seq+1,'prev':'0'*64,'hash':'a'*64})
        with pytest.raises(DBAPIError):db.execute(text('SELECT ras_auth_review_closure_mirror(:l)'),{'l':line})
        db.rollback()
    assert rows(b)==before_rows and b.w.path.read_bytes()==before


@pytest.mark.parametrize('shape',('body_extra','envelope_extra','kind','seq_null','seq_string','hash_null','prev_number','body_null'))
def test_runtime_sql_envelope_and_extra_fields_rejected(writers,shape):
    b=writers;opened(b);before=b.w.path.read_bytes();before_rows=rows(b)
    with Session(b.w.runtime) as db:
        c=staged(b,db);e={'body':c.body(),'kind':'review_closure','seq':journal.read(b.w.path).head.seq+1,'prev':'0'*64,'hash':'a'*64}
        if shape=='body_extra':e['body']['actor']='extra'
        elif shape=='envelope_extra':e['extra']=True
        elif shape=='kind':e['kind']='session_revocation'
        elif shape=='seq_null':e['seq']=None
        elif shape=='seq_string':e['seq']='1'
        elif shape=='hash_null':e['hash']=None
        elif shape=='prev_number':e['prev']=1
        else:e['body']=None
        with pytest.raises(DBAPIError):db.execute(text('SELECT ras_auth_review_closure_mirror(:l)'),{'l':journal.canonical(e)})
        db.rollback()
    assert rows(b)==before_rows and b.w.path.read_bytes()==before


@pytest.mark.parametrize('action',('withdraw','reject'))
def test_real_fsync_fault_full_line_remains_unconfirmed(writers,monkeypatch,action):
    b=writers;opened(b);real=journal.os.fsync
    def fail(*a):raise OSError('injected fsync fault')
    monkeypatch.setattr(journal.os,'fsync',fail)
    r=request(b,action)
    assert r.status_code==503 and r.json()=={'error':'review_journal_unavailable'}
    assert journal.read(b.w.path).head.kind==journal.REVIEW_CLOSURE
    assert recovery.evaluate_integrity(get_settings(),b.w.runtime).reason=='journal_unconfirmed'
    with b.w.engine.connect() as c:
        assert c.scalar(text('SELECT journal_confirmed FROM revocation_journal_records WHERE review_request_id=:r'),{'r':b.rid}) is False
    monkeypatch.setattr(journal.os,'fsync',real)
    recovery.recover(b.w.engine,SimpleNamespace(),get_settings(),NOW)
    assert recovery.evaluate_integrity(get_settings(),b.w.runtime).open


@pytest.mark.parametrize('field',('request_id','item_id','version_id','source_file_id','actor_user_id','status','reason_code','closed_at'))
def test_exact_pending_closure_cannot_bypass_gate(writers,field):
    b=writers;opened(b)
    with Session(b.w.runtime) as db:
        c=staged(b,db)
        value=uuid.uuid4() if field.endswith('_id') else 'rejected' if field=='status' else 'other' if field=='reason_code' else NOW+timedelta(seconds=1)
        with pytest.raises(ApiError):boundary._gate(db,get_settings(),replace(c,**{field:value}))
        db.rollback()


@pytest.mark.parametrize('block',('unjournaled','unconfirmed','hold','ahead'))
def test_pending_gate_preserves_other_integrity_failures(writers,block):
    b=writers;opened(b)
    if block=='hold':recovery.hold(b.w.engine,get_settings(),NOW)
    elif block=='ahead':
        state=journal.read(b.w.path)
        r=journal.next_record(state.head,journal.RECOVERY_BEGIN,dict(recovery_id=str(uuid.uuid4()),started_at=NOW.isoformat(),db_head_seq=state.head.seq))
        journal.append(b.w.path,state,[r])
    elif block=='unconfirmed':
        with b.w.engine.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            conn.execute(text('UPDATE revocation_journal_records SET journal_confirmed=false WHERE seq=(SELECT max(seq) FROM revocation_journal_records)'))
    with Session(b.w.runtime) as db:
        c=staged(b,db)
        if block=='unjournaled':
            # A second local closure is NOT subtracted even on this transaction.
            ids=source(b.w,b.user.id,'second source')
            with b.w.engine.begin() as conn:
                conn.execute(text("SET LOCAL session_replication_role='replica'"))
                conn.execute(text("INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at,closed_at,closed_by_user_id,close_reason_code) VALUES(:i,:v,:f,:u,'withdrawn',:h,0,'test','s','r',:at,:at,:u,'withdrawn_by_requester')"),{'i':ids.item,'v':ids.version,'f':ids.file,'u':b.user.id,'h':SHA,'at':NOW})
        with pytest.raises(ApiError):boundary._gate(db,get_settings(),c)
        db.rollback()


@pytest.mark.parametrize('sql',("INSERT INTO revocation_journal_records(seq) VALUES(999)",
    "UPDATE revocation_journal_records SET journal_confirmed=true",'DELETE FROM revocation_journal_records',
    'CREATE TABLE shadow(x int)','CREATE TEMP TABLE shadow(x int)',"SET ROLE c1_migrator"))
def test_runtime_raw_privilege_denials(installation,sql):
    with installation.runtime.connect() as c,pytest.raises(DBAPIError):c.execute(text(sql))


def test_mirror_confirm_only_and_absent_or_old_target_not_runtime(writers):
    b=writers;opened(b)
    with Session(b.w.runtime) as db:
        c=staged(b,db);state=boundary._gate(db,get_settings(),c)
        record=journal.next_record(state.head,journal.REVIEW_CLOSURE,c.body())
        db.execute(text('SELECT ras_auth_review_closure_mirror(:l)'),{'l':record.line})
        assert not db.scalar(text('SELECT journal_confirmed FROM revocation_journal_records WHERE review_request_id=:r'),{'r':b.rid})
        journal.append(b.w.path,state,[record])
        db.execute(text('SELECT ras_auth_review_closure_confirm(:l)'),{'l':record.line})
        db.commit()
    with Session(b.w.runtime) as db,pytest.raises(DBAPIError):
        db.execute(text('SELECT ras_auth_review_closure_confirm(:l)'),{'l':record.line})


@pytest.mark.parametrize('first',('withdraw','reject'))
def test_two_closers_real_lock_wait_one_closure_no_deadlock(writers,first):
    b=writers;opened(b);entered=threading.Event();release=threading.Event();second_started=threading.Event()
    second='reject' if first=='withdraw' else 'withdraw'
    def run(action,hold=False):
        db=Session(b.w.runtime,expire_on_commit=False)
        real=db.flush
        def flush(*a,**kw):
            changed=any(isinstance(r,ReviewRequest) for r in db.dirty)
            out=real(*a,**kw)
            if hold and changed:
                entered.set();assert release.wait(15)
            return out
        if hold:db.flush=flush
        else:second_started.set()
        try:return direct(b,action,db,boundary.Outcome())['request']['status']
        except ApiError as e:return e.code
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(run,first,True);assert entered.wait(15)
        two=pool.submit(run,second);assert second_started.wait(15)
        assert wait_for(b.w.engine,'transactionid')
        release.set();out=[one.result(20),two.result(20)]
    assert out==['withdrawn' if first=='withdraw' else 'rejected','review_request_closed']
    assert len([r for r in journal.read(b.w.path).records if r.kind==journal.REVIEW_CLOSURE])==1


@pytest.mark.parametrize('action',ACTIONS)
def test_known_commit_new_response_proof_detects_hold(writers,monkeypatch,action):
    b=writers
    if action!='submit':opened(b)
    real=boundary._response
    def response(db,*a,**kw):
        assert not db.in_transaction()
        recovery.hold(b.w.engine,get_settings(),NOW)
        return real(db,*a,**kw)
    monkeypatch.setattr(boundary,'_response',response)
    outcome=boundary.Outcome()
    with Session(b.w.runtime) as db:
        with pytest.raises(ApiError) as caught:direct(b,action,db,outcome)
    assert caught.value.code=='service_recovering'
    assert outcome.committed and outcome.commit_started
    assert rows(b)[0][1]==('open' if action=='submit' else 'withdrawn' if action=='withdraw' else 'rejected')


@pytest.mark.parametrize('action',('withdraw','reject'))
@pytest.mark.parametrize('first',('submit','closure'))
def test_submit_races_closure_both_orders(writers,monkeypatch,action,first):
    b=writers;opened(b);entered=threading.Event();release=threading.Event()
    real=boundary._gate
    def gate(db,settings,pending=None):
        if first=='submit':result=real(db,settings,pending)
        if ((first=='submit' and pending is None) or (first=='closure' and pending is not None)) and not entered.is_set():
            entered.set();assert release.wait(15)
        return result if first=='submit' else real(db,settings,pending)
    monkeypatch.setattr(boundary,'_gate',gate)
    def run(which):
        with Session(b.w.runtime) as db:
            try:return direct(b,which,db,boundary.Outcome())['request']['status']
            except ApiError as e:return e.code
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(run,'submit' if first=='submit' else action);assert entered.wait(15)
        two=pool.submit(run,action if first=='submit' else 'submit')
        # Submit-first holds the exclusive boundary: the second operation's
        # entry integrity connection waits at journal before taking rows.
        # Closure-first holds rows before journal; unique/User waits there.
        expected='advisory' if first=='submit' else 'transactionid'
        try:assert wait_for(b.w.engine,expected)
        finally:release.set()
        result=[one.result(20),two.result(20)]
    assert result==(['review_request_open','withdrawn' if action=='withdraw' else 'rejected'] if first=='submit' else
                    ['withdrawn' if action=='withdraw' else 'rejected','open'])
    assert sum(r[1]=='open' for r in rows(b))==(first=='closure')


def wait_for(engine,wait):
    import time
    for _ in range(100):
        with engine.connect() as c:
            if c.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event=:w"),{'w':wait}):return True
        time.sleep(.03)
    return False


def test_concurrent_submits_one_open_partial_unique(writers):
    b=writers;barrier=threading.Barrier(2)
    def run():
        barrier.wait(10)
        with Session(b.w.runtime,expire_on_commit=False) as db:
            try:return direct(b,'submit',db,boundary.Outcome())['request']['status']
            except ApiError as e:return e.code
    with ThreadPoolExecutor(2) as pool:out=list(pool.map(lambda _:run(),range(2)))
    assert sorted(out)==['open','review_request_open'] and len(rows(b))==1


@pytest.mark.parametrize('action',('withdraw','reject'))
@pytest.mark.parametrize('fault',('before_append','after_append','confirm'))
def test_phase_fault_keeps_intent_outcome_and_no_dto(writers,monkeypatch,action,fault):
    b=writers;opened(b);outcome=boundary.Outcome();db=Session(b.w.runtime)
    real=db.execute;real_append=journal.append
    def execute(stmt,*a,**kw):
        if ('ras_auth_review_closure_mirror' if fault=='before_append' else 'ras_auth_review_closure_confirm') in str(stmt):
            raise RuntimeError('fault stage/confirm')
        return real(stmt,*a,**kw)
    def append(*a,**kw):
        real_append(*a,**kw)
        raise RuntimeError('fault after durable append')
    if fault=='after_append':monkeypatch.setattr(journal,'append',append)
    else:monkeypatch.setattr(db,'execute',execute)
    with pytest.raises(ApiError) as e:direct(b,action,db,outcome)
    assert e.value.code==('review_write_unavailable' if fault=='before_append' else 'review_closure_pending_recovery')
    assert not outcome.commit_started and not outcome.committed
    assert outcome.record is not None
    assert outcome.append_started==(fault!='before_append')
    assert rows(b)[0][1]=='open'
    assert recovery.evaluate_integrity(get_settings(),b.w.runtime).open==(fault=='before_append')


@pytest.mark.parametrize('action',ACTIONS)
@pytest.mark.parametrize('denial',('missing','duplicate','malformed'))
def test_late_resource_denial_rechecks_caller_clock(writers,monkeypatch,action,denial):
    b=writers;opened(b)
    if action!='submit' and denial=='duplicate':assert request(b,action).status_code==200
    before=rows(b);real=boundary._gate
    def gate(*a,**kw):
        result=real(*a,**kw);b.at=NOW+get_settings().auth.session_idle;return result
    monkeypatch.setattr(boundary,'_gate',gate)
    if action=='submit' and denial!='duplicate':b.body=b.body.model_copy(update={'item_id':uuid.uuid4()})
    r=request(b,action,rid=str(b.rid) if denial=='duplicate' else 'malformed' if denial=='malformed' else str(uuid.uuid4()))
    assert r.status_code==401,r.text
    assert rows(b)==before


@pytest.mark.parametrize('time',('infinity','2026-10-07T24:00:00Z','2026-02-30T12:00:00Z','2026-10-07T12:00:00','2026-10-07T12:00:60Z'))
def test_sql_rejects_invalid_time_independently(writers,time):
    b=writers;opened(b);before=b.w.path.read_bytes()
    with Session(b.w.runtime) as db:
        c=staged(b,db);body=c.body()|{'closed_at':time}
        line=journal.canonical(dict(body=body,seq=999,kind='review_closure',prev='0'*64,hash='a'*64))
        with pytest.raises(DBAPIError):db.execute(text('SELECT ras_auth_review_closure_mirror(:l)'),{'l':line})
        db.rollback()
    assert before==b.w.path.read_bytes() and rows(b)[0][1]=='open'


@pytest.mark.parametrize('action',ACTIONS)
@pytest.mark.parametrize('lock',('user','resource','journal'))
@pytest.mark.parametrize('expired',(False,True))
def test_observed_row_and_journal_wait_current_clock(writers,action,lock,expired):
    from app.identity.predicates import lock_users
    from app.models import LibraryItem
    b=writers
    if action!='submit':opened(b)
    entered=threading.Event();release=threading.Event()
    b.after_dependency=lambda db,ctx:(entered.set(),release.wait(15))
    with ThreadPoolExecutor(1) as pool:
        f=pool.submit(request,b,action);assert entered.wait(15)
        with Session(b.w.runtime) as blocker:
            if lock=='user':lock_users(blocker,[b.w.uid if action=='reject' else b.user.id])
            elif lock=='resource':blocker.execute(select(LibraryItem.id).where(LibraryItem.id==b.own.item).with_for_update()) if action=='submit' else blocker.execute(select(ReviewRequest.id).where(ReviewRequest.id==b.rid).with_for_update())
            else:blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'),{'k':recovery.JOURNAL_LOCK})
            release.set();assert wait_for(b.w.engine,'advisory' if lock=='journal' else 'transactionid')
            b.at=NOW+get_settings().auth.session_idle-timedelta(microseconds=0 if expired else 1)
            blocker.rollback()
        r=f.result(20)
    assert r.status_code==(401 if expired else 201 if action=='submit' else 200),r.text
    assert get_engine().pool.checkedout()==0


@pytest.mark.parametrize('action',('withdraw','reject'))
@pytest.mark.parametrize('absent',(False,True))
def test_snapshot_open_runtime_close_restore_recover_never_reopens_grant(writers,admin_engine,app_env,tmp_path,monkeypatch,action,absent):
    from tests.test_recovery import backup
    from tests.conftest import upgrade
    b=writers
    if not absent:opened(b)
    live=SimpleNamespace(engine=b.w.engine)
    with backup(live,admin_engine) as snap:
        if absent:opened(b)
        old=f'/admin/review-requests/{b.rid}/files/{b.own.file}/original'
        r=request(b,action);assert r.status_code==200,r.text
        copied=tmp_path/'current-journal.jsonl';copied.write_bytes(b.w.path.read_bytes())
        monkeypatch.setenv('RAS_RECOVERY__JOURNAL_PATH',str(copied));get_settings.cache_clear()
        settings=get_settings();recovery.hold(snap.engine,settings,NOW)
        upgrade(snap.url)
        assert recovery.evaluate_integrity(settings,snap.engine).reason=='db_behind_journal'
        report=recovery.recover(snap.engine,SimpleNamespace(),settings,NOW)
        assert report.replayed['recorded_request_absent' if absent else 'closed']==1
        raw=copied.read_bytes()
        recovery.recover(snap.engine,SimpleNamespace(),settings,NOW)
        # A second recover may journal its own bounded begin/end; closure is unique.
        assert len([r for r in journal.read(copied).records if r.kind==journal.REVIEW_CLOSURE])==1
        with snap.engine.connect() as c:
            status=c.scalar(text('SELECT status FROM review_requests WHERE id=:r'),{'r':b.rid})
            assert status is None if absent else status=='closed_by_recovery'
            assert c.scalar(text("SELECT count(*) FROM library_items WHERE visibility='common'"))==0
            assert c.scalar(text('SELECT count(*) FROM access_rights'))==0
        app_env(snap.url.set(username=b.w.runtime.url.username,password=b.w.runtime.url.password))
        b.client.cookies.set('ras_session',b.w.actor.token)
        assert b.client.get(old).status_code==404
        detail=b.client.get(f'/admin/review-requests/{b.rid}')
        if absent:assert detail.status_code==404
        else:
            assert detail.status_code==200 and detail.json()['request']['review_available'] is False
            assert 'declaration' not in detail.text


def test_two_users_identical_bytes_private_and_admin_acl(writers):
    b=writers;opened(b)
    user=accept(b.w,issue(b.w,email='user-b@example.test'))
    ids=source(b.w,user.id,'B private identical bytes')
    with Session(b.w.runtime) as db:
        token=login(db,email='user-b@example.test',password=PASSWORD,now=NOW,settings=get_settings(),clock=b.clock)[1]
    b.client.cookies.set('ras_session',token.token);b.client.cookies.set('ras_csrf',token.csrf_token)
    headers={'Origin':'http://web.test','X-CSRF-Token':token.csrf_token}
    assert b.client.post('/review-requests',json=b.body.model_dump(mode='json'),headers=headers).status_code==404
    assert b.client.post(f'/review-requests/{b.rid}/withdraw',headers=headers).status_code==404
    own=b.body.model_dump(mode='json')|{'item_id':str(ids.item),'version_id':str(ids.version),'source_file_id':str(ids.file)}
    r=b.client.post('/review-requests',json=own,headers=headers);assert r.status_code==201,r.text
    assert request(b,'withdraw',rid=r.json()['request']['id']).status_code==404
    assert request(b,'submit',who='admin').status_code==403


@pytest.mark.parametrize('action',('withdraw','reject'))
@pytest.mark.parametrize('first',('auth','closure'))
def test_auth_revocation_races_closure_both_orders(writers,monkeypatch,action,first):
    b=writers;opened(b);entered=threading.Event();release=threading.Event();auth_done=threading.Event()
    p=b.admin if action=='reject' else b.owner
    real_gate=boundary._gate;real_response=boundary._response
    def gate(*a,**kw):
        if first=='closure' and not entered.is_set():entered.set();assert release.wait(15)
        return real_gate(*a,**kw)
    def response(*a,**kw):
        if first=='closure':assert auth_done.wait(15)
        return real_response(*a,**kw)
    monkeypatch.setattr(boundary,'_gate',gate);monkeypatch.setattr(boundary,'_response',response)
    def auth():
        with Session(b.w.runtime) as db:
            revoke_session(db,session_id=p.session_id,user_id=p.user_id,reason=SessionRevokeReason.USER_REVOKED,now=NOW)
            if first=='auth':entered.set();assert release.wait(15)
            commit_auth(db)
        auth_done.set();return 'revoked'
    def closure():
        with Session(b.w.runtime) as db:
            try:return direct(b,action,db,boundary.Outcome())
            except ApiError as e:return e.code
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(auth if first=='auth' else closure);assert entered.wait(15)
        two=pool.submit(closure if first=='auth' else auth)
        try:assert wait_for(b.w.engine,'transactionid')
        finally:release.set()
        out=[one.result(20),two.result(20)]
    assert 'revoked' in out and 'session_expired' in out
    assert rows(b)[0][1]==('open' if first=='auth' else 'withdrawn' if action=='withdraw' else 'rejected')
    assert recovery.evaluate_integrity(get_settings(),b.w.runtime).open


@pytest.mark.parametrize('reader',('history','original'))
@pytest.mark.parametrize('first',('reader','writer'))
def test_runtime_closure_races_get_history_and_original(writers,monkeypatch,reader,first):
    from app.api import original_downloads,review_publication_reads
    from tests.resource_fixtures import BODY
    b=writers;opened(b);entered=threading.Event();release=threading.Event()
    real_gate=boundary._gate;real_caller=original_downloads._caller if reader=='original' else review_publication_reads.final_caller
    final_original=[False]
    real_proof=original_downloads.proof
    def proof(*a,**kw):
        final_original[0]=len(a)>5 or kw.get('expected') is not None
        return real_proof(*a,**kw)
    def caller(*a,**kw):
        if first=='reader' and (reader=='history' or final_original[0]) and not entered.is_set():
            entered.set();assert release.wait(15)
        return real_caller(*a,**kw)
    def gate(*a,**kw):
        if first=='writer' and not entered.is_set():entered.set();assert release.wait(15)
        return real_gate(*a,**kw)
    monkeypatch.setattr(boundary,'_gate',gate)
    if reader=='original':monkeypatch.setattr(original_downloads,'_caller',caller);monkeypatch.setattr(original_downloads,'proof',proof)
    else:monkeypatch.setattr(review_publication_reads,'final_caller',caller)
    b.client.cookies.set('ras_session',b.w.actor.token)
    path=f'/admin/review-requests/{b.rid}'+(f'/files/{b.own.file}/original' if reader=='original' else '')
    def read():return b.client.get(path)
    def write():
        with Session(b.w.runtime) as db:return direct(b,'withdraw',db,boundary.Outcome())
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(read if first=='reader' else write);assert entered.wait(15)
        two=pool.submit(write if first=='reader' else read)
        try:
            assert wait_for(b.w.engine,'transactionid')
        finally:release.set()
        a,z=one.result(20),two.result(20)
    r=a if first=='reader' else z
    if reader=='original':
        assert r.status_code==(200 if first=='reader' else 404),r.text
        if first=='reader':assert r.content==BODY
    else:assert r.json()['request']['review_available']==(first=='reader')
    assert b.client.get(path).status_code==(404 if reader=='original' else 200)


@pytest.mark.parametrize('first',('approve','withdraw'))
def test_withdraw_races_legacy_approval_without_moving_approval_scope(writers,monkeypatch,first):
    from app import promotion
    from app.models.enums import RightsAudience
    from tests.test_c2d2b_review_publication_reads import MemoryOriginals as ApprovalStorage
    b=writers;opened(b);entered=threading.Event();release=threading.Event()
    real_gate=boundary._gate
    storage=ApprovalStorage(b.own)
    rights=promotion.RecordedRights('Synthetic common','Synthetic test author',None,None,'Test permission',NOW.date(),RightsAudience.INSTANCE_USERS,False,False,False,None)
    def gate(*a,**kw):
        if first=='withdraw' and not entered.is_set():entered.set();assert release.wait(15)
        return real_gate(*a,**kw)
    monkeypatch.setattr(boundary,'_gate',gate)
    def approve():
        with Session(b.w.engine,expire_on_commit=False) as db:
            real=db.execute
            def execute(stmt,*a,**kw):
                out=real(stmt,*a,**kw)
                if first=='approve' and 'FOR UPDATE' in str(stmt) and not entered.is_set():entered.set();assert release.wait(15)
                return out
            db.execute=execute
            try:return promotion.approve(db,b.admin,storage,b.rid,SHA,rights,NOW).request.status
            except ApiError as e:return e.code
    def withdraw():
        with Session(b.w.runtime) as db:
            try:return direct(b,'withdraw',db,boundary.Outcome())['request']['status']
            except ApiError as e:return e.code
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(approve if first=='approve' else withdraw);assert entered.wait(15)
        two=pool.submit(withdraw if first=='approve' else approve)
        try:assert wait_for(b.w.engine,'transactionid')
        finally:release.set()
        out=[one.result(20),two.result(20)]
    assert out==['approved' if first=='approve' else 'withdrawn','review_request_closed']
    assert len([r for r in journal.read(b.w.path).records if r.kind==journal.REVIEW_CLOSURE])==1
