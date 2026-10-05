"""C2a direct writer/predicate tests with runtime LOGIN and real journal/guards.

Only cutover is replaced locally. Integrity, hold, SQL guards and commits are real.
"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app import auth_safety, journal, recovery
from app.api.errors import ApiError
from app.config import get_settings
from app.identity import login as login_module
from app.identity.login import login
from app.identity.sessions import (issue_session, resolve_session, session_is_live, list_live_sessions,
                                   rotate_csrf, revoke_session, revoke_user_sessions)
from app.identity.session_commit import commit_auth, AUTH_PENDING
from app.identity.predicates import lock_users, session_live
from app.identity.passwords import hash_password
from app.identity.tokens import digest_matches
from app.models import User, UserSession, AuthChangeReceipt, AuthSafetyHead, RevocationJournalRecord
from app.models.enums import SessionRevokeReason, AuditOutcome, AuditActorType
from app.audit import record_event
from tests.test_auth_safety_foundation import foundation, runtime, append_changes, changes, user, run_recover, NOW, SECRET


@pytest.fixture
def world(foundation, runtime, monkeypatch, app_env):
    w = foundation
    append_changes(w, engine=runtime)
    run_recover(w)
    app_env(runtime.url)
    w.settings = get_settings()
    w.runtime = runtime
    w.clock = lambda: NOW
    # Sole test-only cutover boundary; actual DB pending row is never changed.
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    return w


def sign_in(w, *, previous=None, clock=None, email="c1@example.test", password=SECRET):
    with Session(w.runtime, expire_on_commit=False) as db:
        return login(db, email=email, password=password, now=NOW, settings=w.settings,
                     previous_token=previous, clock=clock or w.clock)[1]


def test_runtime_full_session_lifecycle_and_secrets(world, caplog):
    w=world
    first=sign_in(w)
    second=sign_in(w)
    with Session(w.runtime) as db:
        assert db.scalar(text("SELECT current_user")) == "c1_runtime"
        ctx=resolve_session(db,first.token,now=NOW,idle=w.settings.auth.session_idle,clock=w.clock)
        assert ctx.user_id==w.uid
        assert digest_matches(first.csrf_token,ctx.csrf_token_hash)
        rotated=rotate_csrf(db,first.session_id,user_id=w.uid,clock=w.clock)
        db.commit()
        assert session_is_live(db,first.session_id,now=NOW,idle=w.settings.auth.session_idle)
        assert len(list_live_sessions(db,user_id=w.uid,now=NOW,idle=w.settings.auth.session_idle))==2
        db.rollback()
        assert revoke_user_sessions(db,user_id=w.uid,reason=SessionRevokeReason.USER_REVOKED,
                                    now=NOW,keep_session_id=first.session_id)==1
        commit_auth(db)
        assert not session_is_live(db,second.session_id,now=NOW,idle=w.settings.auth.session_idle)
        db.rollback()
        assert revoke_session(db,session_id=first.session_id,user_id=w.uid,reason=SessionRevokeReason.LOGOUT,now=NOW)
        commit_auth(db)
        assert not session_is_live(db,first.session_id,now=NOW,idle=w.settings.auth.session_idle)
    with Session(w.engine) as db:
        audit=' '.join(db.execute(text("SELECT details::text FROM audit_events")).scalars())
        verifier=db.get(User,w.uid).password_verifier
    for marker in (SECRET,first.token,first.csrf_token,second.token,rotated,verifier):
        assert marker not in w.path.read_text() and marker not in audit and marker not in caplog.text
    assert recovery.evaluate_integrity(w.settings,w.runtime).open


@pytest.mark.parametrize("delta,live", [(timedelta(minutes=29,seconds=59),True),
    (timedelta(minutes=30),False),(timedelta(hours=7,minutes=59,seconds=59),True),(timedelta(hours=8),False)])
def test_expiry_boundaries(world,delta,live):
    w=world;s=sign_in(w)
    if delta>timedelta(hours=1):
        # Real activity routine maintains idle life while absolute time advances.
        for minutes in range(20,480,20):
            at=NOW+timedelta(minutes=minutes)
            with Session(w.runtime) as db:
                assert resolve_session(db,s.token,now=at,idle=w.settings.auth.session_idle,clock=lambda:at)
                db.commit()
    with Session(w.runtime) as db:
        at=NOW+delta
        assert session_is_live(db,s.session_id,now=at,idle=w.settings.auth.session_idle) is live
        db.rollback()
        assert bool(resolve_session(db,s.token,now=at,idle=w.settings.auth.session_idle,clock=lambda:at)) is live


@pytest.mark.parametrize("state",["quarantine","held","disabled","legacy"])
def test_unusable_accounts_generic_login_and_no_session(world,state):
    w=world
    if state=='disabled':
        append_changes(w,changes(w,credential=False,decision='disabled'),engine=w.runtime)
    elif state in ('held','quarantine'):
        append_changes(w,changes(w,credential=state=='quarantine',decision='active' if state=='held' else None),
                       engine=w.runtime,rollback=True)
        run_recover(w)
    else:
        with Session(w.engine) as db:
            u=User(email='legacy@example.test',password_verifier=hash_password(SECRET));db.add(u);db.commit()
    from app.identity.errors import InvalidCredentials
    with pytest.raises(InvalidCredentials):sign_in(w,email='legacy@example.test' if state=='legacy' else 'c1@example.test')
    with Session(w.engine) as db:assert db.scalar(select(UserSession.id)) is None


@pytest.mark.parametrize("domain,decision",[('credential',None),('status','active'),('status','disabled')])
def test_stale_snapshot_head_rejects_even_before_account_restriction(world,domain,decision):
    w=world;s=sign_in(w)
    batch=append_changes(w,changes(w,credential=domain=='credential',decision=decision),engine=w.runtime,rollback=True)
    # Actual replay of mirror/projection, deliberately before restriction. Integrity remains real.
    for entry in batch:recovery.replay_record(w.engine,entry.record,NOW)
    with Session(w.runtime) as db:
        assert not auth_safety.account_live(db,w.uid)
        assert not session_is_live(db,s.session_id,now=NOW,idle=w.settings.auth.session_idle)
        db.rollback()
        assert resolve_session(db,s.token,now=NOW,idle=w.settings.auth.session_idle,clock=w.clock) is None


def add_other(w,role='user'):
    uid=uuid.uuid4()
    with Session(w.engine) as db:
        db.add(User(id=uid,email=f'{uid.hex}@example.test',role=role,password_verifier=hash_password(SECRET),password_changed_at=NOW))
        db.commit()
    other=SimpleNamespace(**vars(w));other.uid=uid
    append_changes(other,engine=w.runtime)
    return other


@pytest.mark.parametrize("role",['user','admin'])
def test_cross_user_session_ownership_csrf_and_replace(world,role):
    w=world;b=add_other(w,role)
    a_session=sign_in(w)
    b_session=sign_in(b,email=f'{b.uid.hex}@example.test')
    with Session(w.runtime) as db:
        assert not revoke_session(db,session_id=a_session.session_id,user_id=b.uid,reason=SessionRevokeReason.USER_REVOKED,now=NOW)
        with pytest.raises(ApiError):rotate_csrf(db,a_session.session_id,user_id=b.uid,clock=w.clock)
        db.rollback()
        assert not session_live(db,a_session.session_id,NOW,w.settings.auth.session_idle,user_id=b.uid)
        assert not digest_matches(b_session.csrf_token,db.get(UserSession,a_session.session_id).csrf_token_hash)
    replacement=sign_in(b,email=f'{b.uid.hex}@example.test',previous=a_session.token)
    with Session(w.runtime) as db:
        assert not session_is_live(db,a_session.session_id,now=NOW,idle=w.settings.auth.session_idle)
        assert session_is_live(db,replacement.session_id,now=NOW,idle=w.settings.auth.session_idle)


def transition(w,domain='credential',decision=None):
    with Session(w.runtime) as db:
        auth_safety.require_writer(db)
        lock_users(db,[w.uid])
        db.info[AUTH_PENDING]=changes(w,credential=domain=='credential',decision=decision,at=NOW+timedelta(seconds=1))
        commit_auth(db)


@pytest.mark.parametrize("domain,decision",[('credential',None),('status','disabled'),('status','active')])
def test_login_verify_race_transition_wins(world,monkeypatch,domain,decision):
    w=world;entered=threading.Event();release=threading.Event();real=login_module.verify_password
    def verify(verifier,password):
        value=real(verifier,password);entered.set();assert release.wait(10);return value
    monkeypatch.setattr(login_module,'verify_password',verify)
    from app.identity.errors import InvalidCredentials
    with ThreadPoolExecutor(1) as pool:
        task=pool.submit(sign_in,w)
        assert entered.wait(10)
        transition(w,domain,decision)
        release.set()
        with pytest.raises(InvalidCredentials):task.result(15)
    with Session(w.engine) as db:assert db.scalar(select(UserSession.id)) is None


def test_login_before_transition_session_generation_fenced(world):
    w=world;s=sign_in(w);transition(w)
    with Session(w.runtime) as db:
        assert not session_is_live(db,s.session_id,now=NOW,idle=w.settings.auth.session_idle)


@pytest.mark.parametrize('damage',['hold','missing','ahead','unconfirmed'])
def test_real_integrity_wins_during_verify(world,monkeypatch,damage):
    w=world;real=login_module.verify_password
    def verify(verifier,password):
        ok=real(verifier,password)
        if damage=='hold':recovery.hold(w.engine,w.settings,NOW)
        elif damage=='missing':w.path.unlink()
        elif damage=='ahead':append_changes(w,changes(w,decision=None),engine=w.runtime,rollback=True)
        else:
            original=journal.append
            def fail(*args):raise OSError('synthetic append failure')
            with monkeypatch.context() as fault:
                fault.setattr(journal,'append',fail)
                append_changes(w,changes(w,decision=None),engine=w.runtime)
        return ok
    monkeypatch.setattr(login_module,'verify_password',verify)
    with pytest.raises(ApiError,match='service_recovering'):sign_in(w)
    with Session(w.engine) as db:assert db.scalar(select(UserSession.id)) is None


@pytest.mark.parametrize('landed',[True,False])
def test_login_commit_lost_reply_never_success(world,monkeypatch,landed):
    w=world
    # Existing cookie causes a real closure batch; fault only the driver boundary.
    old=sign_in(w)
    with Session(w.runtime,expire_on_commit=False) as db:
        commit=db.commit
        def uncertain():
            if landed:commit()
            else:db.rollback()
            raise OSError('synthetic lost commit reply')
        monkeypatch.setattr(db,'commit',uncertain)
        with pytest.raises(ApiError):login(db,email='c1@example.test',password=SECRET,previous_token=old.token,
                                           now=NOW,settings=w.settings,clock=w.clock)
    with Session(w.engine) as db:
        assert db.get(UserSession,old.session_id).revoked_at is not None
        assert db.scalar(text('SELECT count(*) FROM sessions'))==(2 if landed else 1)


@pytest.mark.parametrize('shape',['none','torn','prefix','full'])
def test_mixed_batch_all_false_before_append_fault(world,monkeypatch,shape):
    w=world;s=sign_in(w);original=journal.append
    with Session(w.runtime) as db:
        lock_users(db,[w.uid])
        assert revoke_session(db,session_id=s.session_id,user_id=w.uid,reason=SessionRevokeReason.LOGOUT,now=NOW)
        db.info[AUTH_PENDING]=changes(w,decision=None)
        record_event(db,action='auth.test.close',outcome=AuditOutcome.SUCCESS,actor_type=AuditActorType.USER,actor_user_id=w.uid)
        def fail(path,state,records):
            assert [r.kind for r in records]==[journal.CREDENTIAL_TRANSITION,journal.SESSION_REVOCATION]
            assert db.scalar(text('SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed'))==2
            if shape=='torn':
                with path.open('ab') as stream:stream.write(records[0].line[:20].encode())
            elif shape=='prefix':original(path,state,records[:1])
            elif shape=='full':
                with monkeypatch.context() as fault:
                    def fsync(fd):raise OSError('synthetic fsync')
                    fault.setattr(journal.os,'fsync',fsync);original(path,state,records)
            raise OSError('synthetic append')
        with monkeypatch.context() as fault:
            fault.setattr(journal,'append',fail)
            with pytest.raises(ApiError,match='auth_journal_unavailable'):commit_auth(db)
    assert not recovery.evaluate_integrity(w.settings,w.runtime).open
    with Session(w.engine) as db:
        assert db.get(UserSession,s.session_id).revoked_at is not None
        assert db.scalar(text('SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed'))==2
    run_recover(w,confirm_db_current=True)
    assert recovery.evaluate_integrity(w.settings,w.runtime).open


def test_rehash_real_password_policy_and_no_new_event(world):
    w=world;before=user(w);lines=w.path.read_bytes()
    with w.engine.begin() as conn:
        conn.execute(text('INSERT INTO auth_hash_policies SELECT 2,memory_kib,time_cost+1,parallelism,salt_bytes,hash_bytes FROM auth_hash_policies WHERE version=1'))
    sign_in(w)
    after=user(w)
    assert after.password_hash_policy_version==2 and after.password_verifier!=before.password_verifier
    assert (after.credential_generation,after.credential_operation_id,after.password_changed_at)==(
        before.credential_generation,before.credential_operation_id,before.password_changed_at)
    assert w.path.read_bytes()==lines


@pytest.mark.parametrize('sql',[
    'UPDATE sessions SET last_activity_at=now()', 'UPDATE sessions SET csrf_token_hash=repeat(\'a\',64)',
    'UPDATE sessions SET revoked_at=now()', 'INSERT INTO revocation_journal_records(seq) VALUES(999)',
    'CREATE TEMP TABLE x(id int)', 'ALTER TABLE sessions DISABLE TRIGGER ALL', 'SET ROLE c1_migrator'])
def test_runtime_raw_writer_denied(world,sql):
    sign_in(world)
    with world.runtime.begin() as conn,pytest.raises(DBAPIError):conn.execute(text(sql))


def test_http_session_csrf_logout_with_scoped_cutover_fixture(world,monkeypatch):
    from app.main import app
    from app.api.deps import get_clock, get_now
    from fastapi.testclient import TestClient
    w=world
    monkeypatch.setenv('RAS_ENVIRONMENT','development')
    monkeypatch.setenv('RAS_HTTP__COOKIE_SECURE','false')
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS','["http://web.test"]')
    get_settings.cache_clear()
    app.dependency_overrides[get_clock]=lambda:w.clock
    app.dependency_overrides[get_now]=lambda:NOW
    try:
        with TestClient(app,base_url='http://web.test') as client:
            assert client.get('/auth/csrf').status_code==200
            token=client.cookies.get('ras_csrf')
            headers={'origin':'http://web.test','x-csrf-token':token}
            assert client.post('/auth/login',json={'email':'c1@example.test','password':SECRET},headers=headers).status_code==200
            assert client.get('/auth/session').status_code==200
            assert client.get('/auth/sessions').json()['sessions'][0]['current']
            token=client.cookies.get('ras_csrf')
            assert client.post('/auth/logout',headers={'origin':'http://web.test','x-csrf-token':'wrong'}).status_code==403
            assert client.post('/auth/logout',headers={'origin':'http://web.test','x-csrf-token':token}).status_code==204
            assert client.get('/auth/session').status_code==401
    finally:
        app.dependency_overrides.clear();get_settings.cache_clear()


@pytest.mark.parametrize('outcome',['live','revoked','idle_expired','absolute_expired','credential_changed'])
def test_http_session_listing_rechecks_caller_after_dependency(world,monkeypatch,outcome):
    from fastapi import Depends, Request
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_clock, get_now, get_db, get_session_context
    from app.api.cookies import session_cookie_name
    w=world
    moment=SimpleNamespace(now=NOW)
    clock=lambda:moment.now
    caller=sign_in(w)
    if outcome=='absolute_expired':
        # Keep idle life using the real runtime routine; absolute life stays fixed.
        for minutes in range(20,480,20):
            at=NOW+timedelta(minutes=minutes)
            with Session(w.runtime) as db:
                assert resolve_session(db,caller.token,now=at,idle=w.settings.auth.session_idle,clock=lambda:at)
                db.commit()
        moment.now=caller.absolute_expires_at-timedelta(seconds=1)
    other=sign_in(w,clock=clock)
    authenticated,continue_handler=threading.Event(),threading.Event()
    def pause_after_auth(request: Request, db: Session=Depends(get_db),
                         now=Depends(get_now), settings=Depends(get_settings), clock=Depends(get_clock)):
        ctx=get_session_context(request,db=db,now=now,settings=settings,clock=clock)
        assert ctx.session_id==caller.session_id
        assert not db.in_transaction()  # dependency releases its final journal/read locks
        authenticated.set()
        assert continue_handler.wait(15)
        return ctx
    monkeypatch.setenv('RAS_ENVIRONMENT','development')
    monkeypatch.setenv('RAS_HTTP__COOKIE_SECURE','false')
    get_settings.cache_clear()
    app.dependency_overrides[get_session_context]=pause_after_auth
    app.dependency_overrides[get_clock]=lambda:clock
    app.dependency_overrides[get_now]=clock
    try:
        with TestClient(app,base_url='http://web.test') as client,ThreadPoolExecutor(max_workers=1) as pool:
            client.cookies.set(session_cookie_name(get_settings()),caller.token)
            response=pool.submit(client.get,'/auth/sessions')
            try:
                assert authenticated.wait(15)
                if outcome=='revoked':
                    with Session(w.runtime) as db:
                        assert revoke_session(db,session_id=caller.session_id,user_id=w.uid,
                                              reason=SessionRevokeReason.USER_REVOKED,now=clock())
                        commit_auth(db)
                elif outcome=='credential_changed':
                    append_changes(w,changes(w,decision=None),engine=w.runtime)
                    other=sign_in(w,clock=clock)
                elif outcome=='idle_expired':
                    at=NOW+w.settings.auth.session_idle-timedelta(seconds=1)
                    with Session(w.runtime) as db:
                        assert resolve_session(db,other.token,now=at,idle=w.settings.auth.session_idle,clock=lambda:at)
                        db.commit()
                    moment.now=NOW+w.settings.auth.session_idle
                elif outcome=='absolute_expired':
                    moment.now=caller.absolute_expires_at
                with Session(w.runtime) as db:
                    assert session_live(db,other.session_id,clock(),w.settings.auth.session_idle,user_id=w.uid)
            finally:
                continue_handler.set()
            result=response.result(15)
            if outcome=='live':
                assert result.status_code==200
                listed=result.json()['sessions']
                assert {s['id'] for s in listed}=={str(caller.session_id),str(other.session_id)}
                assert [s['id'] for s in listed if s['current']]==[str(caller.session_id)]
            else:
                assert result.status_code==401
                assert result.json()=={'error':'session_expired'}
                assert str(other.session_id) not in result.text
                assert 'sessions' not in result.json()
    finally:
        continue_handler.set()
        app.dependency_overrides.clear();get_settings.cache_clear()


def test_http_login_uncertain_no_cookie_success(world,monkeypatch):
    from app.main import app
    from app.api.deps import get_clock, get_now, get_db
    from fastapi.testclient import TestClient
    w=world
    monkeypatch.setenv('RAS_ENVIRONMENT','development')
    monkeypatch.setenv('RAS_HTTP__COOKIE_SECURE','false');monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS','["http://web.test"]')
    get_settings.cache_clear()
    def broken_db():
        with Session(w.runtime) as db:
            commit=db.commit
            def lost_reply():commit();raise OSError('synthetic lost reply')
            monkeypatch.setattr(db,'commit',lost_reply)
            yield db
    app.dependency_overrides[get_db]=broken_db
    app.dependency_overrides[get_clock]=lambda:w.clock
    app.dependency_overrides[get_now]=lambda:NOW
    try:
        with TestClient(app,base_url='http://web.test') as client:
            client.cookies.set('ras_csrf','synthetic-csrf')
            response=client.post('/auth/login',json={'email':'c1@example.test','password':SECRET},
                                 headers={'origin':'http://web.test','x-csrf-token':'synthetic-csrf'})
            assert response.status_code==503
            assert not response.headers.get_list('set-cookie')
            assert client.cookies.get('ras_session') is None
    finally:app.dependency_overrides.clear();get_settings.cache_clear()


def test_previous_session_expiry_during_hash_and_final_clock(world,monkeypatch):
    w=world;old=sign_in(w);real=login_module.verify_password
    at=NOW
    def verify(verifier,password):
        nonlocal at
        ok=real(verifier,password);at=NOW+timedelta(minutes=30);return ok
    monkeypatch.setattr(login_module,'verify_password',verify)
    new=sign_in(w,previous=old.token,clock=lambda:at)
    with Session(w.runtime) as db:
        assert db.get(UserSession,old.session_id).revoked_at is None # expired cookie is not replaced
        assert session_is_live(db,new.session_id,now=at,idle=w.settings.auth.session_idle)


def test_final_clock_expired_after_known_commit(world,monkeypatch):
    w=world;at=NOW
    with Session(w.runtime,expire_on_commit=False) as db:
        commit=db.commit
        def committed():
            nonlocal at
            commit();at=NOW+timedelta(hours=8)
        monkeypatch.setattr(db,'commit',committed)
        with pytest.raises(ApiError,match='session_expired'):
            login(db,email='c1@example.test',password=SECRET,now=NOW,settings=w.settings,clock=lambda:at)


def test_hold_after_known_commit_blocks_success(world,monkeypatch):
    w=world
    with Session(w.runtime,expire_on_commit=False) as db:
        commit=db.commit
        def held():commit();recovery.hold(w.engine,w.settings,NOW)
        monkeypatch.setattr(db,'commit',held)
        with pytest.raises(ApiError,match='service_recovering'):
            login(db,email='c1@example.test',password=SECRET,now=NOW,settings=w.settings,clock=w.clock)


def test_session_cross_generation_and_legacy_null(world):
    w=world;b=add_other(w)
    with w.runtime.begin() as conn,pytest.raises(DBAPIError,match='session credential fence mismatch'):
        conn.execute(text('SELECT ras_auth_session_create(:id,:uid,:gen,:token,:csrf,:at,:end)'),
                     {'id':uuid.uuid4(),'uid':w.uid,'gen':user(b).credential_generation,'token':'a'*64,'csrf':'b'*64,'at':NOW,'end':NOW+timedelta(hours=8)})
    with Session(w.engine) as db:
        row=UserSession(user_id=w.uid,token_hash='a'*64,csrf_token_hash='b'*64,created_at=NOW,
                        last_activity_at=NOW,absolute_expires_at=NOW+timedelta(hours=8))
        db.add(row);db.commit();sid=row.id
    with Session(w.runtime) as db:
        assert not session_is_live(db,sid,now=NOW,idle=w.settings.auth.session_idle)
        assert not list_live_sessions(db,user_id=w.uid,now=NOW,idle=w.settings.auth.session_idle)


def test_actor_expiry_while_waiting_final_check_rolls_back_closure(world):
    w=world;s=sign_in(w)
    with Session(w.runtime) as db:
        revoke_session(db,session_id=s.session_id,user_id=w.uid,reason=SessionRevokeReason.LOGOUT,now=NOW)
        row=db.get(UserSession,s.session_id)
        db.info['auth_actor']=(s.session_id,w.uid,row.csrf_token_hash,lambda:NOW+timedelta(minutes=30),w.settings.auth.session_idle)
        before=w.path.read_bytes()
        with pytest.raises(ApiError,match='session_expired'):commit_auth(db)
    assert w.path.read_bytes()==before
    with Session(w.engine) as db:assert db.get(UserSession,s.session_id).revoked_at is None


def test_cross_replace_and_touch_close_no_deadlock(world):
    w=world;b=add_other(w);a=sign_in(w);s=sign_in(b,email=f'{b.uid.hex}@example.test')
    barrier=threading.Barrier(2)
    def replace(owner,email,old):
        barrier.wait(10);return sign_in(owner,email=email,previous=old)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(replace,w,'c1@example.test',s.token)
        second=pool.submit(replace,b,f'{b.uid.hex}@example.test',a.token)
        assert first.result(15) and second.result(15)
    fresh=sign_in(w)
    barrier=threading.Barrier(2)
    def touch():
        barrier.wait(10)
        with Session(w.runtime) as db:
            ctx=resolve_session(db,fresh.token,now=NOW,idle=w.settings.auth.session_idle,clock=w.clock)
            db.commit();return ctx
    def close():
        barrier.wait(10)
        with Session(w.runtime) as db:
            revoke_session(db,session_id=fresh.session_id,user_id=w.uid,reason=SessionRevokeReason.LOGOUT,now=NOW)
            commit_auth(db)
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(touch);b=pool.submit(close);a.result(15);b.result(15)
    with Session(w.runtime) as db:assert not session_is_live(db,fresh.session_id,now=NOW,idle=w.settings.auth.session_idle)


@pytest.mark.parametrize('decision',['active','disabled'])
def test_login_waiting_user_lock_transition_wins(world,decision):
    import time
    w=world
    with Session(w.runtime) as winner:
        lock_users(winner,[w.uid])
        winner.info[AUTH_PENDING]=changes(w,credential=False,decision=decision)
        with ThreadPoolExecutor(1) as pool:
            task=pool.submit(sign_in,w)
            deadline=time.monotonic()+10
            observed=False
            while time.monotonic()<deadline:
                with w.engine.connect() as observer:
                    observed=bool(observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND usename='c1_runtime' AND wait_event='transactionid')")))
                if observed:break
                threading.Event().wait(.01)
            assert observed, 'login must actually be waiting on User row lock'
            commit_auth(winner)
            from app.identity.errors import InvalidCredentials
            with pytest.raises(InvalidCredentials):task.result(15)


@pytest.mark.parametrize('shape',['none','torn','prefix','full'])
@pytest.mark.parametrize('landed',[False,True])
def test_mixed_commit_uncertain_fault_matrix(world,monkeypatch,shape,landed):
    w=world;s=sign_in(w);original=journal.append
    with Session(w.runtime) as db:
        lock_users(db,[w.uid])
        revoke_session(db,session_id=s.session_id,user_id=w.uid,reason=SessionRevokeReason.LOGOUT,now=NOW)
        db.info[AUTH_PENDING]=changes(w,decision=None)
        def fail(path,state,records):
            if shape=='torn':
                with path.open('ab') as stream:stream.write(records[0].line[:20].encode())
            elif shape=='prefix':original(path,state,records[:1])
            elif shape=='full':
                with monkeypatch.context() as fault:
                    def fsync(fd):raise OSError('synthetic fsync')
                    fault.setattr(journal.os,'fsync',fsync);original(path,state,records)
            raise OSError('synthetic append')
        commit=db.commit
        def uncertain():
            if landed:commit()
            else:db.rollback()
            raise OSError('synthetic lost reply')
        with monkeypatch.context() as fault:
            fault.setattr(journal,'append',fail);fault.setattr(db,'commit',uncertain)
            with pytest.raises(ApiError):commit_auth(db)
    run_recover(w,confirm_db_current=landed)
    u=user(w)
    if landed:assert u.credential_state=='usable'
    elif shape in ('prefix','full'):assert u.credential_state=='quarantined'
    with Session(w.engine) as db:
        assert (db.get(UserSession,s.session_id).revoked_at is not None)==(landed or shape=='full')
    assert recovery.evaluate_integrity(w.settings,w.runtime).open


def test_authenticated_principals_preserve_private_item_file_chunk_acl(world):
    from app.models import LibraryItem, PaperVersion, SourceFile, Section, Chunk
    from app.authz import Principal, m2a_private_derivatives_clause
    from app.api.routes.library import list_items, get_chunks, Scope
    w=world;owner=add_other(w);other=add_other(w)
    item_id=uuid.uuid4()
    with Session(w.engine) as db:
        db.add(LibraryItem(id=item_id,visibility='private',owner_user_id=owner.uid,title='private paper marker'));db.flush()
        version=PaperVersion(item_id=item_id);db.add(version);db.flush()
        file=SourceFile(item_id=item_id,version_id=version.id,storage_key='originals/'+uuid.uuid4().hex,
                        verified_mime='text/markdown',byte_size=20,checksum_sha256='a'*64)
        db.add(file);db.flush()
        loc=dict(locator_kind='markdown',start_offset=0,end_offset=20,start_line=1,end_line=1,pipeline_version='synthetic-v1')
        section=Section(item_id=item_id,version_id=version.id,source_file_id=file.id,kind='body',ordinal=0,text='private body marker',**loc)
        db.add(section);db.flush()
        chunk=Chunk(section_id=section.id,item_id=item_id,version_id=version.id,source_file_id=file.id,ordinal=0,
                    text='private body marker',chunk_hash='a'*64,**loc)
        db.add(chunk);db.commit();fid=file.id
    for person,allowed in [(owner,True),(other,False),(w,False)]:
        issued=sign_in(person,email=f'{person.uid.hex}@example.test' if person is not w else 'c1@example.test')
        with Session(w.runtime) as db:
            ctx=resolve_session(db,issued.token,now=NOW,idle=w.settings.auth.session_idle,clock=w.clock)
            db.commit()
            p=Principal(ctx.user_id,ctx.role,ctx.session_id,ctx.email,ctx.display_name)
            result=list_items(scope=Scope.ALL,limit=20,offset=0,principal=p,db=db,
                              clock=w.clock,settings=w.settings)
            assert result['total']==(1 if allowed else 0)
            files=list(db.scalars(select(SourceFile.id).join(LibraryItem,LibraryItem.id==SourceFile.item_id)
                                 .where(m2a_private_derivatives_clause(p))))
            assert files==([fid] if allowed else [])
            if allowed:
                assert get_chunks(str(item_id),limit=20,offset=0,principal=p,db=db,
                                  clock=w.clock,settings=w.settings)['chunks'][0]['text']=='private body marker'
            else:
                with pytest.raises(ApiError,match='not_found'):
                    get_chunks(str(item_id),limit=20,offset=0,principal=p,db=db,
                               clock=w.clock,settings=w.settings)


def test_final_helper_fresh_clock_and_cross_user_receipt_guard(world):
    w=world;s=sign_in(w);other=add_other(w)
    with Session(w.runtime) as db:
        assert not session_is_live(db,s.session_id,now=NOW,idle=w.settings.auth.session_idle,
                                   clock=lambda:NOW+timedelta(hours=8))
    with pytest.raises(DBAPIError),w.engine.begin() as conn:
        conn.execute(text('UPDATE users SET status_operation_id=:op,status_generation=:gen WHERE id=:uid'),
                     {'op':user(other).status_operation_id,'gen':user(other).status_generation,'uid':w.uid})


@pytest.mark.parametrize('domain,decision',[('credential',None),('status','active'),('status','disabled')])
def test_confirmed_latest_mirror_without_projection_or_receipt_denies(world,domain,decision):
    from tests.test_auth_safety_foundation import event
    w=world;s=sign_in(w)
    kind=journal.CREDENTIAL_TRANSITION if domain=='credential' else journal.ACCOUNT_STATUS_DECISION
    body=event(kind,w.uid)
    if decision:body.update(decision=decision,reason_code='admin_enable' if decision=='active' else 'admin_disable')
    with Session(w.runtime) as db:
        lock_users(db,[w.uid]);recovery._lock_journal(db)
        state=journal.read(w.path);record=journal.next_record(state.head,kind,body)
        db.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"),{'line':record.line})
        journal.append(w.path,state,[record])
        db.execute(text('SELECT ras_auth_confirm(:seq)'),{'seq':record.seq});db.commit()
    assert recovery.evaluate_integrity(w.settings,w.runtime).open
    with Session(w.runtime) as db:
        assert not auth_safety.account_live(db,w.uid)
        assert not session_is_live(db,s.session_id,now=NOW,idle=w.settings.auth.session_idle)
    from app.identity.errors import InvalidCredentials
    with pytest.raises(InvalidCredentials):sign_in(w)


@pytest.mark.parametrize('domain,decision',[('credential',None),('status','disabled')])
def test_login_hash_race_with_real_policy_upgrade(world,monkeypatch,domain,decision):
    w=world
    with w.engine.begin() as conn:
        conn.execute(text('INSERT INTO auth_hash_policies SELECT 2,memory_kib,time_cost+1,parallelism,salt_bytes,hash_bytes FROM auth_hash_policies WHERE version=1'))
    entered=threading.Event();release=threading.Event();factory=login_module.hasher_for_policy
    class BarrierHasher:
        def __init__(self,p):self.real=factory(p)
        def check_needs_rehash(self,v):return self.real.check_needs_rehash(v)
        def hash(self,password):
            hashed=self.real.hash(password);entered.set();assert release.wait(10);return hashed
    monkeypatch.setattr(login_module,'hasher_for_policy',BarrierHasher)
    from app.identity.errors import InvalidCredentials
    with ThreadPoolExecutor(1) as pool:
        task=pool.submit(sign_in,w)
        assert entered.wait(10);transition(w,domain,decision);release.set()
        with pytest.raises(InvalidCredentials):task.result(15)


@pytest.mark.parametrize('landed',[False,True])
def test_http_logout_uncertain_no_success_or_new_cookie(world,monkeypatch,landed):
    from app.main import app
    from app.api.deps import get_clock,get_now,get_db
    from fastapi.testclient import TestClient
    w=world;s=sign_in(w)
    monkeypatch.setenv('RAS_ENVIRONMENT','development');monkeypatch.setenv('RAS_HTTP__COOKIE_SECURE','false')
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS','["http://web.test"]');get_settings.cache_clear()
    def driver_boundary():
        with Session(w.runtime) as db:
            commit=db.commit
            def commit_reply():
                closing=db.scalar(select(UserSession.revoked_at).where(UserSession.id==s.session_id)) is not None
                if not closing:commit();return
                if landed:commit()
                else:db.rollback()
                raise OSError('synthetic lost reply')
            monkeypatch.setattr(db,'commit',commit_reply);yield db
    app.dependency_overrides[get_db]=driver_boundary
    app.dependency_overrides[get_clock]=lambda:w.clock;app.dependency_overrides[get_now]=lambda:NOW
    try:
        with TestClient(app,base_url='http://web.test') as client:
            client.cookies.set('ras_session',s.token);client.cookies.set('ras_csrf',s.csrf_token)
            response=client.post('/auth/logout',headers={'origin':'http://web.test','x-csrf-token':s.csrf_token})
            assert response.status_code==503
            assert not response.headers.get_list('set-cookie')
        with Session(w.engine) as db:
            assert db.get(UserSession,s.session_id).revoked_at is not None
            audit=db.scalar(text("SELECT count(*) FROM audit_events WHERE action='auth.logout'"))
            assert audit==(1 if landed else 0) # closure replay never reconstructs success audit
    finally:app.dependency_overrides.clear();get_settings.cache_clear()
