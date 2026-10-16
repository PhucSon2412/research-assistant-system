"""Real runtime LOGIN and journal; only the pending cutover boundary is scoped."""
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from app import auth_safety, journal, recovery
from app.api.errors import ApiError
from app.config import get_settings
from app.identity import invitations as invite
from app.identity.bootstrap import bootstrap_admin
from app.identity.errors import InvalidToken
from app.identity.invitation_endings import PENDING, lock_email, lock_invitations
from app.identity.login import login
from app.identity.passwords import hash_password
from app.identity.session_commit import AUTH_PENDING, commit_auth
from app.identity.tokens import hash_token
from app.models import User, Invitation, AuthChangeReceipt
from tests.test_c2c1_bootstrap import installation
from tests.test_auth_safety_foundation import NOW, SECRET

EMAIL = 'c2c2-private-email@example.test'
PASSWORD = 'C2c2-private-password-789!'


@pytest.fixture
def world(installation, monkeypatch):
    monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
    yield from _seed_world(installation)


@pytest.fixture
def operational_world(installation):
    """C4 business/process setup; C2c historical fault isolation keeps world above."""
    recovery.recover(installation.engine,SimpleNamespace(),installation.settings,NOW)
    yield from _seed_world(installation)


def _seed_world(w):
    w.clock = lambda: NOW
    w.uid = bootstrap_admin(w.runtime, email='admin@example.test', password=SECRET,
                            policy=w.settings.password, clock=w.clock).user_id
    with Session(w.runtime, expire_on_commit=False) as db:
        w.actor = login(db, email='admin@example.test', password=SECRET, now=NOW,
                        settings=w.settings, clock=w.clock)[1]
    import os
    w.recovery = create_engine(w.runtime.url.set(username='c1_recovery',password=os.environ['RAS_C1_RECOVERY_PASSWORD']),hide_parameters=True)
    yield w
    w.recovery.dispose()


def issue(w, *, email=EMAIL, clock=None):
    with Session(w.runtime, expire_on_commit=False) as db:
        result = invite.create_invitation(db, inviter_id=w.uid, actor_session_id=w.actor.session_id,
            csrf_token=w.actor.csrf_token, email=email, display_name='private-display-marker', now=NOW,
            lifetime=w.settings.auth.invite, clock=clock or w.clock)
        db.expunge(result.invitation)
        return result


def accept(w, created, *, clock=None):
    with Session(w.runtime, expire_on_commit=False) as db:
        result = invite.accept_invitation(db, raw_token=created.token, password=PASSWORD, display_name=None,
                                         now=NOW, policy=w.settings.password, clock=clock or w.clock)
        db.expunge(result)
        return result


def revoke(w, created, *, clock=None, csrf=None):
    with Session(w.runtime) as db:
        return invite.revoke_invitation(db, invitation_id=created._binding.invitation_id, actor_id=w.uid,
            actor_session_id=w.actor.session_id, csrf_token=csrf if csrf is not None else w.actor.csrf_token,
            now=NOW, clock=clock or w.clock)


def deliver(w, created, adapter, *, clock=None):
    with Session(w.runtime) as db:
        result = invite.deliver_invitation(db, created, actor_id=w.uid, actor_session_id=w.actor.session_id,
            csrf_token=w.actor.csrf_token, delivery=adapter, settings=w.settings, clock=clock or w.clock)
        status = invite.invitation_status(result, (clock or w.clock)())
        return status


def count(w, table):
    with w.runtime.connect() as db:
        return db.scalar(text(f'SELECT count(*) FROM {table}'))


def recover(w, **kw):
    return recovery.recover(w.recovery, SimpleNamespace(), w.settings, NOW, **kw)


def test_pair_login_initial_status_and_secrets(world, caplog):
    from app.identity.users import _initial_status_verified, set_user_status
    from app.models.enums import UserStatus
    w = world
    caplog.set_level(logging.DEBUG, logger='sqlalchemy.engine.Engine')
    w.runtime.echo='debug'
    created = issue(w)
    user = accept(w, created)
    with Session(w.runtime) as db:
        assert auth_safety.account_live(db, user.id) and _initial_status_verified(db, user.id)
        inv = db.get(Invitation,created._binding.invitation_id)
        receipts = list(db.scalars(select(AuthChangeReceipt).where(AuthChangeReceipt.user_id==user.id)))
        assert len(receipts)==2 and {r.domain for r in receipts}=={'credential','status'}
        assert all(r.transaction_id==inv.acceptance_transaction_id and r.reason_code=='invitation_accept' for r in receipts)
        audit=' '.join(db.execute(text('SELECT row_to_json(a)::text FROM audit_events a')).scalars())
    assert user.role=='user' and count(w,'sessions')==1
    records=journal.read(w.path).records[-3:]
    assert [r.kind for r in records]==[journal.CREDENTIAL_TRANSITION,journal.ACCOUNT_STATUS_DECISION,journal.INVITATION_CLOSURE]
    assert records[-1].body['invitation_id']==str(created._binding.invitation_id)
    for marker in (created.token,hash_token(created.token),PASSWORD,user.password_verifier,EMAIL,'private-display-marker'):
        assert marker not in caplog.text+audit+w.path.read_text()+repr(created)
    w.runtime.echo=False
    with Session(w.runtime) as db:
        assert login(db,email=EMAIL,password=PASSWORD,now=NOW,settings=w.settings,clock=w.clock)[1].session_id
    for status in (UserStatus.DISABLED,UserStatus.ACTIVE):
        with Session(w.runtime) as db:
            set_user_status(db,user_id=user.id,actor_id=w.uid,status=status,now=NOW,
                actor_session_id=w.actor.session_id,csrf_token=w.actor.csrf_token,clock=w.clock)


@pytest.mark.parametrize('role',['c1_runtime','c1_recovery'])
@pytest.mark.parametrize('privilege',['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'])
def test_no_raw_invitation_grants(world, role, privilege):
    with world.runtime.connect() as db:
        assert not db.scalar(text("SELECT has_table_privilege(:r,'invitations',:p)"),{'r':role,'p':privilege})


@pytest.mark.parametrize('sql', ["UPDATE invitations SET display_name='bypass'",'DELETE FROM invitations',
 'TRUNCATE invitations',"INSERT INTO invitations(email) VALUES ('bypass@example.test')",'INSERT INTO users(email) VALUES (NULL)'])
def test_runtime_raw_writes_refused(world,sql):
    issue(world)
    with world.runtime.begin() as db, pytest.raises(DBAPIError): db.execute(text(sql))


def prepare(db, created, **changes):
    params={'id':created._binding.invitation_id,'token':hash_token(created.token),'email':EMAIL,'uid':uuid.uuid4(),
            'name':None,'v':hash_password(PASSWORD),'p':1,'at':NOW}|changes
    db.execute(text('SELECT ras_auth_invitation_prepare(:id,:token,:email,:uid,:name,:v,:p,:at)'),params)
    return params


def changes(params):
    return [auth_safety.Change(params['uid'],'credential','invitation_accept',params['at'],verifier=params['v'],policy_version=params['p']),
            auth_safety.Change(params['uid'],'status','invitation_accept',params['at'],decision='active')]


@pytest.mark.parametrize('bad',[{'id':None},{'id':uuid.uuid4()},{'token':None},{'token':'0'*64},{'email':None},
 {'email':'wrong@example.test'},{'uid':None},{'uid':uuid.UUID(int=1)},{'v':None},{'v':'PHC-private-invalid-marker'},
 {'p':None},{'p':99},{'at':None},{'at':NOW+timedelta(hours=72)}])
def test_direct_prepare_invalid_binding_no_intent(world,bad):
    w=world; created=issue(w); before=w.path.read_bytes()
    with Session(w.runtime) as db,pytest.raises(DBAPIError): prepare(db,created,**bad)
    assert count(w,'users')==1 and w.path.read_bytes()==before


@pytest.mark.parametrize('shape',['none','credential','status','wrong_reason','wrong_time','wrong_user','no_closure','wrong_closure'])
def test_pair_binding_checked_before_append(world, monkeypatch, shape):
    w=world; created=issue(w); before=w.path.read_bytes(); called=[]
    monkeypatch.setattr(journal,'append',lambda *a: called.append(a))
    with Session(w.runtime) as db:
        params=prepare(db,created); values=changes(params)
        if shape=='none': values=[]
        if shape=='credential': values=values[:1]
        if shape=='status': values=values[1:]
        if shape=='wrong_reason': values[-1]=replace(values[-1],reason_code='admin_enable')
        if shape=='wrong_time': values[-1]=replace(values[-1],recorded_at=NOW+timedelta(seconds=1))
        if shape=='wrong_user': values[-1]=replace(values[-1],user_id=w.uid)
        row=db.get(Invitation,created._binding.invitation_id,populate_existing=True)
        db.info[PENDING]=[] if shape=='no_closure' else [row]
        if shape=='wrong_closure':
            # A different invitation closure cannot satisfy this acceptance.
            row=Invitation(id=uuid.uuid4(),accepted_at=NOW,closure_reason='invitation_accepted')
            db.info[PENDING]=[row]
        db.info[AUTH_PENDING]=values
        with pytest.raises(ApiError): commit_auth(db)
    assert not called and count(w,'users')==1 and w.path.read_bytes()==before


def test_direct_prepare_valid_runtime_pair(world):
    w=world; created=issue(w)
    with Session(w.runtime) as db:
        params=prepare(db,created)
        db.info[AUTH_PENDING]=changes(params)
        db.info[PENDING]=[db.get(Invitation,created._binding.invitation_id,populate_existing=True)]
        batch=commit_auth(db)
        assert len(batch)==3 and all(r.written for r in batch)
        assert auth_safety.account_live(db,params['uid'])
    assert count(w,'users')==2


@pytest.mark.parametrize('phase',['hash','journal'])
def test_ttl_fresh_after_slow_work(world,monkeypatch,phase):
    w=world; created=issue(w); current=[NOW]
    if phase=='hash':
        real=invite.hasher_for_policy
        def factory(policy):
            h=real(policy)
            class Hasher:
                def hash(self,password):
                    assert w.runtime.pool.checkedout()==0
                    current[0]=NOW+timedelta(hours=72)
                    return h.hash(password)
            return Hasher()
        monkeypatch.setattr(invite,'hasher_for_policy',factory)
    else:
        real=invite.commit_auth
        def commit(db,**kw):
            current[0]=NOW+timedelta(hours=72)
            return real(db,**kw)
        monkeypatch.setattr(invite,'commit_auth',commit)
    with pytest.raises(InvalidToken): accept(w,created,clock=lambda:current[0])
    assert count(w,'users')==1


@pytest.mark.parametrize('shape',['none','torn','credential','status','full','ok'])
@pytest.mark.parametrize('outcome',['commit','rollback','uncertain_commit','uncertain_rollback'])
def test_accept_fault_matrix(world,monkeypatch,shape,outcome):
    w=world; created=issue(w); real_append=journal.append; real_commit=Session.commit
    def fault(path,state,records):
        assert len(records)==3
        if shape=='ok': return real_append(path,state,records)
        if shape=='torn':
            with path.open('ab') as s:s.write(records[0].line[:25].encode());s.flush()
        if shape in ('credential','status'): real_append(path,state,records[:1 if shape=='credential' else 2])
        if shape=='full':
            with monkeypatch.context() as p:
                p.setattr(journal.os,'fsync',lambda fd:(_ for _ in ()).throw(OSError('synthetic fsync')))
                real_append(path,state,records)
        raise OSError('synthetic append')
    def commit(db):
        if outcome in ('commit','uncertain_commit'): real_commit(db)
        else: db.rollback()
        if outcome!='commit': raise OSError('synthetic commit reply')
    with monkeypatch.context() as p:
        p.setattr(journal,'append',fault);p.setattr(Session,'commit',commit)
        if shape=='ok' and outcome=='commit': accept(w,created)
        else:
            with pytest.raises(ApiError): accept(w,created)
    landed=outcome in ('commit','uncertain_commit')
    assert count(w,'users')==1+int(landed)
    assert count(w,'auth_change_receipts')==2+2*int(landed)
    with w.runtime.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed'))==(3 if landed and shape!='ok' else 0)
    if landed or shape!='none':
        recover(w,confirm_db_current=landed)
        recover(w)
    with Session(w.runtime) as db:
        row=db.get(Invitation,created._binding.invitation_id)
        if landed: assert row.accepted_user_id and row.accepted_at
        elif shape in ('full','ok'): assert row.closed_by_recovery_at and row.accepted_user_id is None
        else: assert row.accepted_at is row.closed_by_recovery_at is None
    if not landed and shape not in ('full','ok'):
        assert accept(w,created).role=='user'  # prefix without closure does not close unrelated invitation


@pytest.mark.parametrize('action',['error','expire','accept_error','accept_expire','revoke_error','replace_error'])
def test_delivery_compensation_races_and_no_locks(world, action):
    w=world; created=issue(w); current=[NOW]
    class Adapter:
        enabled=True
        def send(self,message,now):
            assert w.runtime.pool.checkedout()==0
            with w.runtime.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout='100ms'"))
                conn.execute(text('SELECT ras_auth_lock_users(:ids)'),{'ids':[w.uid]})
                lock_email(Session(bind=conn),EMAIL)
                conn.execute(text('SELECT ras_auth_invitation_lock(:ids)'),{'ids':[created._binding.invitation_id]})
                conn.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':recovery.JOURNAL_LOCK})
            if action.startswith('accept'): accept(w,created)
            if action=='revoke_error': revoke(w,created)
            if action=='replace_error': issue(w)
            if 'expire' in action: current[0]=NOW+w.settings.auth.session_idle
            if 'error' in action: raise OSError('adapter may already have sent')
    with pytest.raises(ApiError): deliver(w,created,Adapter(),clock=lambda:current[0])
    with Session(w.runtime) as db:
        row=db.get(Invitation,created._binding.invitation_id)
        if action.startswith('accept'):
            assert row.accepted_at and row.accepted_user_id and not row.revoked_at
            assert auth_safety.account_live(db,row.accepted_user_id)
        else:
            assert row.revoked_at and not row.accepted_at
            expected={'revoke_error':'admin_revoked','replace_error':'invitation_replaced'}.get(action,'delivery_failed')
            assert row.closure_reason==expected


def test_accept_accept_barrier(world,monkeypatch):
    w=world;created=issue(w);barrier=threading.Barrier(2);real=invite.hasher_for_policy
    def factory(policy):
        h=real(policy)
        class H:
            def hash(self,password):
                value=h.hash(password);barrier.wait(15);return value
        return H()
    monkeypatch.setattr(invite,'hasher_for_policy',factory)
    def attempt():
        try: accept(w,created);return 'ok'
        except InvalidToken:return 'invalid'
    with ThreadPoolExecutor(2) as pool:
        jobs=[pool.submit(attempt) for _ in range(2)]
        assert sorted(j.result(30) for j in jobs)==['invalid','ok']
    assert count(w,'users')==2


@pytest.mark.parametrize('phase',['create','revoke','list'])
@pytest.mark.parametrize('change',['live','idle','absolute','revoke','disable','credential','role','csrf'])
def test_http_actor_after_real_dependency(world,monkeypatch,phase,change):
    from fastapi import Depends, Request
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_clock,get_now,get_session_context
    from app.identity.sessions import revoke_session
    from app.models.enums import SessionRevokeReason
    from tests.helpers import DEV_ENV
    w=world;created=issue(w);at=[NOW];clock=lambda:at[0]
    entered=threading.Event();release=threading.Event()
    def pause(request:Request,db=Depends(__import__('app.api.deps',fromlist=['get_db']).get_db),
              settings=Depends(get_settings)):
        ctx=get_session_context(request,db=db,now=NOW,settings=settings,clock=clock)
        assert not db.in_transaction();entered.set();assert release.wait(15);return ctx
    for k,v in DEV_ENV.items():monkeypatch.setenv(k,v)
    get_settings.cache_clear()
    app.dependency_overrides[get_clock]=lambda:clock
    app.dependency_overrides[get_now]=clock
    app.dependency_overrides[get_session_context]=pause
    try:
        with TestClient(app,base_url='http://web.test') as client,ThreadPoolExecutor(1) as pool:
            client.cookies.set('ras_session',w.actor.token);client.cookies.set('ras_csrf',w.actor.csrf_token)
            headers={'origin':'http://web.test','x-csrf-token':w.actor.csrf_token}
            if phase=='create': job=pool.submit(client.post,'/admin/invitations',json={'email':'next@example.test'},headers=headers)
            elif phase=='revoke':job=pool.submit(client.post,f'/admin/invitations/{created._binding.invitation_id}/revoke',headers=headers)
            else:job=pool.submit(client.get,'/admin/invitations')
            try:
                assert entered.wait(15)
                if change=='idle':at[0]=NOW+w.settings.auth.session_idle
                elif change=='absolute':at[0]=NOW+w.settings.auth.session_absolute
                elif change=='revoke':
                    with Session(w.runtime) as db:
                        revoke_session(db,session_id=w.actor.session_id,user_id=w.uid,reason=SessionRevokeReason.USER_REVOKED,now=NOW)
                        commit_auth(db)
                elif change in ('disable','credential'):
                    with Session(w.runtime) as db:
                        from app.identity.predicates import lock_users
                        lock_users(db,[w.uid])
                        db.info[AUTH_PENDING]=[auth_safety.Change(w.uid,'status','admin_disable',NOW,decision='disabled')
                            if change=='disable' else auth_safety.Change(w.uid,'credential','password_reset',NOW,verifier=hash_password(SECRET),policy_version=1)]
                        commit_auth(db)
                elif change=='role':
                    with w.engine.begin() as db:db.execute(text("UPDATE users SET role='user' WHERE id=:id"),{'id':w.uid})
                elif change=='csrf':
                    from app.identity.sessions import rotate_csrf
                    with Session(w.runtime) as db:
                        rotate_csrf(db,w.actor.session_id,user_id=w.uid,clock=clock);db.commit()
            finally: release.set()
            result=job.result(30)
        expected=(201 if phase=='create' else 204 if phase=='revoke' else 200) if change=='live' or (change=='csrf' and phase=='list') else 403 if change in ('role','csrf') else 401
        assert result.status_code==expected,result.text
        if expected>=400:
            assert 'email' not in result.text and 'private-display-marker' not in result.text
            assert count(w,'invitations')==1
            with Session(w.runtime) as db: assert db.get(Invitation,created._binding.invitation_id).revoked_at is None
    finally:
        app.dependency_overrides.clear();get_settings.cache_clear()


@pytest.mark.parametrize('winner',['accept','replace','revoke','compensate','hold'])
def test_accept_waiting_hash_loses_to_winner(world,monkeypatch,winner):
    w=world;created=issue(w);entered=threading.Event();release=threading.Event();real=invite.hasher_for_policy
    def factory(policy):
        h=real(policy)
        class H:
            def hash(self,password):
                value=h.hash(password);entered.set();assert release.wait(15);return value
        return H()
    with monkeypatch.context() as patch,ThreadPoolExecutor(1) as pool:
        patch.setattr(invite,'hasher_for_policy',factory)
        job=pool.submit(accept,w,created)
        try:
            assert entered.wait(15)
            patch.setattr(invite,'hasher_for_policy',real)
            if winner=='accept': accept(w,created)
            elif winner=='replace':issue(w)
            elif winner=='revoke':revoke(w,created)
            elif winner=='compensate':
                with Session(w.runtime) as db:invite._compensate_delivery(db,created,w.clock)
            else:recovery.hold(w.recovery,w.settings,NOW)
        finally: release.set()
        with pytest.raises(ApiError if winner=='hold' else InvalidToken):job.result(30)
    assert count(w,'users')==2 if winner=='accept' else count(w,'users')==1


def test_reinvite_reinvite_barrier(world):
    w=world;first=issue(w);barrier=threading.Barrier(2)
    def create():barrier.wait(15);return issue(w)
    with ThreadPoolExecutor(2) as pool:
        a,b=[j.result(30) for j in [pool.submit(create),pool.submit(create)]]
    with Session(w.runtime) as db:
        rows=list(db.scalars(select(Invitation).where(Invitation.email==EMAIL)))
        assert len(rows)==3 and sum(r.revoked_at is None for r in rows)==1
        for r in rows:
            if r.revoked_at: assert r.closure_reason=='invitation_replaced'


@pytest.mark.parametrize('phase',['before','after'])
@pytest.mark.parametrize('change',['idle','revoke','disable','credential','role','csrf','hold'])
def test_delivery_actor_gate_and_compensation(world,phase,change):
    w=world;created=issue(w);at=[NOW];sent=[]
    def mutate():
        if change=='idle':at[0]=NOW+w.settings.auth.session_idle
        elif change=='hold':recovery.hold(w.recovery,w.settings,NOW)
        elif change in ('disable','credential'):
            from app.identity.predicates import lock_users
            with Session(w.runtime) as db:
                lock_users(db,[w.uid]);db.info[AUTH_PENDING]=[auth_safety.Change(w.uid,'status','admin_disable',NOW,decision='disabled')
                    if change=='disable' else auth_safety.Change(w.uid,'credential','password_reset',NOW,verifier=hash_password(SECRET),policy_version=1)]
                commit_auth(db)
        elif change=='revoke':
            from app.identity.sessions import revoke_session
            from app.models.enums import SessionRevokeReason
            with Session(w.runtime) as db:
                revoke_session(db,session_id=w.actor.session_id,user_id=w.uid,reason=SessionRevokeReason.USER_REVOKED,now=NOW);commit_auth(db)
        elif change=='role':
            with w.engine.begin() as db:db.execute(text("UPDATE users SET role='user' WHERE id=:id"),{'id':w.uid})
        else:
            from app.identity.sessions import rotate_csrf
            with Session(w.runtime) as db:rotate_csrf(db,w.actor.session_id,user_id=w.uid,clock=w.clock);db.commit()
    class Adapter:
        enabled=True
        def send(self,message,now):sent.append(message);mutate()
    if phase=='before':mutate()
    with pytest.raises(ApiError):deliver(w,created,Adapter(),clock=lambda:at[0])
    assert len(sent)==int(phase=='after')
    with Session(w.runtime) as db:
        row=db.get(Invitation,created._binding.invitation_id)
        if change=='hold':assert row.revoked_at is None
        else:assert row.closure_reason=='delivery_failed'


@pytest.mark.parametrize('outcome',['rollback','uncertain_commit'])
def test_create_unknown_commit_never_reaches_delivery(world,monkeypatch,outcome):
    w=world;real=Session.commit
    def commit(db):
        if outcome=='uncertain_commit':real(db)
        else:db.rollback()
        raise OSError('lost reply')
    monkeypatch.setattr(Session,'commit',commit)
    with pytest.raises(ApiError,match='auth_outcome_uncertain'):issue(w)
    assert count(w,'invitations')==int(outcome=='uncertain_commit')


def test_accepted_mirror_all_false_before_first_byte(world,monkeypatch):
    w=world;created=issue(w);real=journal.append;record= recovery.record_auth_endings;current=[]
    def capture(db,*a,**kw):current.append(db);return record(db,*a,**kw)
    def append(path,state,records):
        db=current[-1]
        assert len(records)==3 and db.scalar(text('SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed'))==3
        assert db.scalar(text("SELECT count(*) FROM auth_change_receipts WHERE reason_code='invitation_accept'"))==2
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='invitation.accept'"))==1
        return real(path,state,records)
    monkeypatch.setattr(recovery,'record_auth_endings',capture);monkeypatch.setattr(journal,'append',append)
    accept(w,created)


def test_actor_expires_at_final_commit(world,monkeypatch):
    w=world;at=[NOW];real=invite.commit_auth
    def commit(db,**kw):
        at[0]=NOW+w.settings.auth.session_idle
        return real(db,**kw)
    monkeypatch.setattr(invite,'commit_auth',commit)
    with pytest.raises(ApiError,match='session_expired'):issue(w,clock=lambda:at[0])
    assert count(w,'invitations')==0


@pytest.mark.parametrize('lock',['email','row','journal'])
def test_waiting_accept_rechecks_clock_at_observed_db_wait(world,monkeypatch,lock):
    import time
    w=world;created=issue(w);at=[NOW];entered=threading.Event();release=threading.Event()
    if lock=='journal':
        real=invite.commit_auth
        def commit(db,**kw):entered.set();assert release.wait(15);return real(db,**kw)
        monkeypatch.setattr(invite,'commit_auth',commit)
    else:
        real=invite.hasher_for_policy
        def factory(policy):
            h=real(policy)
            class H:
                def hash(self,password):
                    value=h.hash(password);entered.set();assert release.wait(15);return value
            return H()
        monkeypatch.setattr(invite,'hasher_for_policy',factory)
    with w.runtime.connect() as blocker,ThreadPoolExecutor(1) as pool:
        job=pool.submit(accept,w,created,clock=lambda:at[0])
        assert entered.wait(15)
        if lock=='email':lock_email(Session(bind=blocker),EMAIL)
        elif lock=='row':blocker.execute(text('SELECT ras_auth_invitation_lock(:ids)'),{'ids':[created._binding.invitation_id]})
        else:blocker.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':recovery.JOURNAL_LOCK})
        release.set()
        try:
            deadline=time.monotonic()+10
            with w.engine.connect() as observer:
                while time.monotonic()<deadline:
                    waiting=observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database()"
                        " AND usename='c1_runtime' AND wait_event=:event)"),{'event':'transactionid' if lock=='row' else 'advisory'})
                    if waiting:break
                    observer.rollback();threading.Event().wait(.005)
                else:raise AssertionError('expected database lock wait was not observed')
            at[0]=NOW+timedelta(hours=72)
        finally:blocker.rollback();release.set()
        with pytest.raises(InvalidToken):job.result(20)
    assert count(w,'users')==1


@pytest.mark.parametrize('phase',['before','after'])
def test_delivery_ttl_boundary(world,phase):
    w=world;created=issue(w);at=[NOW];sent=[]
    class Adapter:
        enabled=True
        def send(self,message,now):sent.append(message);at[0]=NOW+timedelta(hours=72)
    if phase=='before':at[0]=NOW+timedelta(hours=72)
    # An actor kept live at the token boundary isolates invitation TTL from session TTL.
    w.settings.auth.session_absolute=timedelta(days=4)
    w.settings.auth.session_idle=timedelta(days=4)
    with w.engine.begin() as db:
        db.execute(text("UPDATE sessions SET absolute_expires_at=:at WHERE id=:id"),
                   {'at':NOW+timedelta(days=4),'id':w.actor.session_id})
    with pytest.raises(ApiError,match='invitation_unavailable'):deliver(w,created,Adapter(),clock=lambda:at[0])
    assert len(sent)==int(phase=='after')


def test_recovery_no_creation_grants(world):
    w=world;created=issue(w)
    with Session(w.recovery) as db,pytest.raises(DBAPIError,match='permission denied'):
        prepare(db,created)
    assert count(w,'users')==1


@pytest.mark.parametrize('field',['token_hash','transaction_id','inviter_id','invitation_id'])
def test_compensation_binding_not_reusable(world,field):
    w=world;created=issue(w)
    value={'token_hash':'0'*64,'transaction_id':created._binding.transaction_id+1,'inviter_id':uuid.uuid4(),'invitation_id':uuid.uuid4()}[field]
    forged=replace(created,_binding=replace(created._binding,**{field:value}))
    with Session(w.runtime) as db,pytest.raises(ApiError):invite._compensate_delivery(db,forged,w.clock)
    with Session(w.runtime) as db:assert db.get(Invitation,created._binding.invitation_id).revoked_at is None


@pytest.mark.parametrize('mutate',["UPDATE users SET role='admin' WHERE id=:uid",
 "UPDATE invitations SET acceptance_transaction_id=NULL WHERE id=:id",
 "UPDATE invitations SET accepted_user_id=:actor WHERE id=:id",
 "DELETE FROM auth_change_receipts WHERE user_id=:uid",
 "UPDATE invitations SET acceptance_transaction_id=acceptance_transaction_id+1 WHERE id=:id"])
def test_db_binding_wrong_role_marker_user_receipt(world,monkeypatch,mutate):
    w=world;created=issue(w);before=w.path.read_bytes();called=[]
    # Privileged setup corrupts one binding in the transaction, exercising the SQL
    # fence rather than simply re-testing the runtime raw-write prohibition.
    with Session(w.engine) as db:
        params=prepare(db,created);values=changes(params)
        state=journal.read(w.path)
        records=auth_safety.stage(db,state,values)
        row=db.get(Invitation,created._binding.invitation_id,populate_existing=True)
        closure=journal.next_record(records[-1],journal.INVITATION_CLOSURE,recovery.invitation_body(row))
        db.execute(text("SELECT ras_auth_closure_mirror(:line,'close','invitation_ended')"),{'line':closure.line})
        with pytest.raises(DBAPIError):
            db.execute(text(mutate),{'id':params['id'],'uid':params['uid'],'actor':w.uid})
            db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        db.rollback()
    assert w.path.read_bytes()==before and count(w,'users')==1


@pytest.mark.parametrize('shape',['actor','target','time','action','missing'])
def test_sql_accept_audit_binding_before_first_byte(world,monkeypatch,shape):
    w=world;created=issue(w);before=w.path.read_bytes();called=[]
    with w.engine.begin() as db:
        sql=db.scalar(text("SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname='ras_auth_invitation_prepare'"))
        old="VALUES(p_at,'user',p_user,'invitation.accept','success','invitation',i.id::text);"
        new={'actor':"VALUES(p_at,'user',i.invited_by_user_id,'invitation.accept','success','invitation',i.id::text);",
             'target':"VALUES(p_at,'user',p_user,'invitation.accept','success','invitation',i.invited_by_user_id::text);",
             'time':"VALUES(p_at+interval '1 second','user',p_user,'invitation.accept','success','invitation',i.id::text);",
             'action':"VALUES(p_at,'user',p_user,'invitation.wrong','success','invitation',i.id::text);"}.get(shape)
        assert old in sql
        if shape=='missing':
            sql=sql.replace("INSERT INTO audit_events(occurred_at,actor_type,actor_user_id,action,outcome,target_type,target_id)\n "+old,'')
        else:sql=sql.replace(old,new)
        db.execute(text(sql))
    monkeypatch.setattr(journal,'append',lambda *a:called.append(a))
    with pytest.raises(ApiError):accept(w,created)
    assert not called and w.path.read_bytes()==before and count(w,'users')==1


@pytest.mark.parametrize('field',['user_id','operation_id','generation'])
def test_uuid_from_absent_rollback_history_cannot_be_reused(world,field):
    w=world;created=issue(w)
    with Session(w.runtime) as db:
        p=prepare(db,created);state=journal.read(w.path);records=auth_safety.stage(db,state,changes(p))
        journal.append(w.path,state,records[:1]);db.rollback()
    recover(w)
    assert count(w,'users')==1
    with Session(w.runtime) as db,pytest.raises(DBAPIError):prepare(db,created,uid=uuid.UUID(records[0].body[field]))
    assert accept(w,created).role=='user'


@pytest.mark.parametrize('flow',['accept','replace','revoke'])
def test_clock_expires_during_append_no_commit_success(world,monkeypatch,flow):
    w=world;created=issue(w);at=[NOW];real=journal.append
    def slow(path,state,records):
        value=real(path,state,records)
        at[0]=NOW+(timedelta(hours=72) if flow=='accept' else w.settings.auth.session_idle)
        return value
    with monkeypatch.context() as p:
        p.setattr(journal,'append',slow)
        with pytest.raises(InvalidToken if flow=='accept' else ApiError):
            if flow=='accept':accept(w,created,clock=lambda:at[0])
            elif flow=='replace':issue(w,clock=lambda:at[0])
            else:revoke(w,created,clock=lambda:at[0])
    assert count(w,'users')==1 and count(w,'invitations')==1
    assert not recovery.evaluate_integrity(w.settings,w.runtime).open
    recover(w);recover(w)
    with Session(w.runtime) as db:
        row=db.get(Invitation,created._binding.invitation_id)
        assert row.closed_by_recovery_at and row.accepted_user_id is None


@pytest.mark.parametrize('change',['ttl','closed'])
def test_inspect_rechecks_before_disclosing_identity(world,monkeypatch,change):
    w=world;created=issue(w);at=[NOW]
    real=invite.find_open_invitation
    def read(db,*a,**kw):
        row=real(db,*a,**kw)
        at[0]=NOW+timedelta(hours=72)
        return row
    if change=='closed':revoke(w,created)
    else:monkeypatch.setattr(invite,'find_open_invitation',read)
    with Session(w.runtime) as db,pytest.raises(InvalidToken):invite.inspect_invitation(db,created.token,clock=lambda:at[0])


def test_disabled_delivery_never_calls_adapter_and_compensates_known_link(world):
    w=world;created=issue(w)
    class Disabled:
        enabled=False
        def send(self,*a,**kw):raise AssertionError('disabled adapter must not be called')
    with pytest.raises(ApiError,match='delivery_unavailable'):deliver(w,created,Disabled())
    with Session(w.runtime) as db:assert db.get(Invitation,created._binding.invitation_id).closure_reason=='delivery_failed'
