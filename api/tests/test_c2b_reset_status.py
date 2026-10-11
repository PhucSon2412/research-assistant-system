"""Runtime LOGIN, PostgreSQL/journal/guards real; only pending boundary replaced."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app.config import get_settings
from app import auth_safety, journal, recovery
from app.api.errors import ApiError
from app.identity import password_reset as reset
from app.identity.users import set_user_status
from app.identity.errors import InvalidToken
from app.identity.passwords import verify_password, PasswordPolicyError
from app.identity.sessions import session_is_live, revoke_session
from app.identity.session_commit import commit_auth
from app.models import User, PasswordResetToken, UserSession, AuthChangeReceipt, AuthSafetyHead
from app.models.enums import UserStatus, SessionRevokeReason
from tests.test_auth_safety_foundation import foundation, runtime, append_changes, changes, user, run_recover, NOW, SECRET
from tests.test_c2a_login_sessions import sign_in, transition

NEW = "C2b-new-private-password-marker-456!"


@pytest.fixture
def world(foundation, runtime, monkeypatch, app_env):
    w = foundation
    values = changes(w)
    # Initial status evidence from a generic runtime reason; verified_initial_status needs a C3 case.
    values[-1] = replace(values[-1], reason_code="invitation_accept")
    append_changes(w, values, engine=runtime)
    run_recover(w)
    app_env(runtime.url)
    w.settings = get_settings()
    w.runtime = runtime
    w.clock = lambda: NOW
    # Only the pending boundary is replaced; initial receipts/guards remain real.
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    return w


def add_other(w, role="user", *, initial_reason="invitation_accept"):
    from app.identity.passwords import hash_password
    uid = uuid.uuid4()
    with Session(w.engine) as db:
        db.add(User(id=uid, email=f"{uid.hex}@example.test", role=role,
                    password_verifier=hash_password(SECRET), password_changed_at=NOW))
        db.commit()
    other = SimpleNamespace(**vars(w)); other.uid = uid
    operator = initial_reason == "verified_initial_status"
    values = changes(other, decision="active" if initial_reason and not operator else None)
    if initial_reason and not operator:
        values[-1] = replace(values[-1], reason_code=initial_reason)
    append_changes(other, values, engine=w.runtime)
    if operator:
        # Only a real C3 operator case can mint this reason (0049); recover closes the hold afterwards.
        from tests.test_c3_operator_recovery import operator_initial_status
        operator_initial_status(w, uid)
    return other


@pytest.mark.parametrize("history", ["credential_only", "admin_enable", "uncommitted_initial"])
def test_disable_cannot_supply_initial_status_proof(world, history):
    from app.identity.errors import InvalidCredentials
    w = world; actor = sign_in(w); target = add_other(w, initial_reason=None)
    if history != "credential_only":
        change = auth_safety.Change(target.uid, "status",
            "invitation_accept" if history == "uncommitted_initial" else "admin_enable",
            NOW, decision="active")
        append_changes(target, [change], engine=w.runtime, rollback=history == "uncommitted_initial")
        run_recover(w)
    before = user(target)
    assert before.credential_state == "usable"
    if history == "credential_only":
        assert before.status_safety == "held"
    with pytest.raises(ApiError, match="initial_status_verification_required"):
        status(w, target, actor, UserStatus.ACTIVE)
    status(w, target, actor, UserStatus.DISABLED)
    with Session(w.runtime) as db:
        receipts = db.scalar(text("SELECT count(*) FROM auth_change_receipts"))
        audits = db.scalar(text("SELECT count(*) FROM audit_events"))
    journal_before = w.path.read_bytes()
    with pytest.raises(ApiError, match="initial_status_verification_required") as denied:
        status(w, target, actor, UserStatus.ACTIVE)
    assert denied.value.status_code == 409
    assert w.path.read_bytes() == journal_before
    after = user(target)
    assert after.status == "disabled" and after.credential_operation_id == before.credential_operation_id
    with Session(w.runtime) as db:
        assert db.scalar(text("SELECT count(*) FROM auth_change_receipts")) == receipts
        assert db.scalar(text("SELECT count(*) FROM audit_events")) == audits
        assert db.scalar(select(UserSession.id).where(UserSession.user_id == target.uid)) is None
    with pytest.raises(InvalidCredentials):
        sign_in(target, email=f"{target.uid.hex}@example.test")
    assert recovery.evaluate_integrity(w.settings, w.runtime).open


@pytest.mark.parametrize("initial_reason", ["bootstrap", "invitation_accept", "verified_initial_status"])
def test_verified_initial_status_survives_disable_enable(world, initial_reason):
    w = world; actor = sign_in(w); target = add_other(w, initial_reason=initial_reason)
    status(w, target, actor, UserStatus.DISABLED)
    status(w, target, actor, UserStatus.ACTIVE)
    # Historical initial receipt is sufficient even when latest reason is admin_enable.
    assert user(target).status == "active"
    assert sign_in(target, email=f"{target.uid.hex}@example.test").session_id


def issue(w, *, clock=None, quarantine=False):
    with Session(w.runtime) as db:
        if quarantine:
            return reset._issue(db, email="c1@example.test", lifetime=w.settings.auth.password_reset,
                clock=clock or w.clock, purpose="quarantine")
        return reset.request_reset(db, email="c1@example.test", now=NOW, lifetime=w.settings.auth.password_reset,
                                   clock=clock or w.clock)


def confirm(w, token, password=NEW, clock=None):
    with Session(w.runtime) as db:
        return reset.confirm_reset(db, raw_token=token.token, new_password=password, now=NOW,
                                   policy=w.settings.password, clock=clock or w.clock)


def status(w, target, actor_session, decision, *, clock=None, csrf=None):
    with Session(w.runtime) as db:
        return set_user_status(db, user_id=target.uid, actor_id=w.uid, status=decision, now=NOW,
            actor_session_id=actor_session.session_id, csrf_token=csrf if csrf is not None else actor_session.csrf_token,
            clock=clock or w.clock)


def quarantine(w):
    append_changes(w, changes(w, decision=None), engine=w.runtime, rollback=True)
    run_recover(w)
    assert user(w).credential_state == "quarantined"


def test_reset_runtime_lifecycle_receipt_batch_secrets(world, caplog):
    w=world; s=sign_in(w); before=user(w); first=issue(w); token=issue(w)
    with Session(w.runtime) as db:
        old=db.get(PasswordResetToken,first.token_id)
        assert old.revoked_at==NOW and old.closure_reason=="reset_replaced"
        assert not reset.token_live(db,first.token_id,w.uid,NOW)
        assert reset.token_live(db,token.token_id,w.uid,NOW)
    assert confirm(w,token)==w.uid
    after=user(w)
    assert after.credential_generation!=before.credential_generation
    assert after.status_operation_id==before.status_operation_id
    assert verify_password(after.password_verifier,NEW)
    assert not verify_password(after.password_verifier,SECRET)
    with Session(w.runtime) as db:
        assert db.get(AuthChangeReceipt,after.credential_operation_id).reason_code=="password_reset"
        assert db.get(PasswordResetToken,token.token_id).used_at==NOW
        assert not session_is_live(db,s.session_id,now=NOW,idle=w.settings.auth.session_idle)
        audit=" ".join(db.execute(text("SELECT details::text FROM audit_events")).scalars())
    with pytest.raises(InvalidToken):confirm(w,token)
    for marker in (token.token,first.token,NEW,SECRET,after.password_verifier,s.token,s.csrf_token):
        assert marker not in w.path.read_text() and marker not in audit and marker not in caplog.text
    assert recovery.evaluate_integrity(w.settings,w.runtime).open


def test_quarantine_latest_fence_rejects_old_password_and_public_request(world):
    w=world; before=user(w); quarantine(w)
    assert issue(w) is None
    token=issue(w,quarantine=True)
    with Session(w.runtime) as db:
        row=db.get(PasswordResetToken,token.token_id);head=db.get(AuthSafetyHead,w.uid)
        assert row.credential_fence_operation_id==head.credential_operation_id!=before.credential_operation_id
    with pytest.raises(PasswordPolicyError):confirm(w,token,SECRET)
    assert confirm(w,token)==w.uid
    assert user(w).credential_state=="usable" and user(w).status=="active"


@pytest.mark.parametrize("state",["disabled","held","legacy","newer_credential","newer_status"])
@pytest.mark.parametrize("purpose",["normal","quarantine"])
def test_reset_eligibility_denies_noncurrent_status_and_credential(world,state,purpose):
    w=world
    if state=="disabled":transition(w,"status","disabled")
    elif state=="held":
        append_changes(w,changes(w,credential=False),engine=w.runtime,rollback=True);run_recover(w)
    elif state=="legacy":
        with Session(w.engine) as db:
            from app.identity.passwords import hash_password
            u=User(email="legacy@example.test",password_verifier=hash_password(SECRET));db.add(u);db.commit();uid=u.id
        with Session(w.runtime) as db:
            assert not reset.eligible(db,uid,uuid.uuid4(),purpose)
        return
    else:
        batch=append_changes(w,changes(w,credential=state=="newer_credential",decision="active" if state=="newer_status" else None),
                             engine=w.runtime,rollback=True)
        for entry in batch:recovery.replay_record(w.engine,entry.record,NOW)
    assert issue(w,quarantine=purpose=="quarantine") is None


@pytest.mark.parametrize("outcome",["credential","status","replacement","expiry","hold"])
def test_reset_hash_race_uses_fresh_snapshot_clock_and_integrity(world,monkeypatch,outcome):
    w=world;token=issue(w);entered=threading.Event();release=threading.Event();at=[NOW]
    real=reset.hasher_for_policy
    class Paused:
        def __init__(self,policy):self.hasher=real(policy)
        def hash(self,pw):
            value=self.hasher.hash(pw);entered.set();assert release.wait(15);return value
    monkeypatch.setattr(reset,"hasher_for_policy",Paused)
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(confirm,w,token,NEW,lambda:at[0]);assert entered.wait(15)
        if outcome=="credential":transition(w)
        elif outcome=="status":transition(w,"status","disabled")
        elif outcome=="replacement":issue(w)
        elif outcome=="expiry":at[0]=NOW+w.settings.auth.password_reset
        else:recovery.hold(w.engine,w.settings,NOW)
        release.set()
        with pytest.raises(ApiError if outcome=="hold" else InvalidToken):job.result(20)
    assert not verify_password(user(w).password_verifier,NEW)


def test_hash_runs_without_read_transaction(world,monkeypatch):
    w=world;token=issue(w)
    with Session(w.runtime) as db:
        real=reset.hasher_for_policy
        def check(policy):
            assert not db.in_transaction()
            return real(policy)
        monkeypatch.setattr(reset,"hasher_for_policy",check)
        reset.confirm_reset(db,raw_token=token.token,new_password=NEW,now=NOW,policy=w.settings.password,clock=w.clock)


@pytest.mark.parametrize("sessions",[False,True])
def test_disable_enable_journal_without_sessions_preserves_credential(world,sessions):
    w=world;actor=sign_in(w);target=add_other(w);before=user(target)
    ts=sign_in(target,email=f"{target.uid.hex}@example.test") if sessions else None
    with Session(w.runtime) as db:
        token=reset.request_reset(db,email=f"{target.uid.hex}@example.test",now=NOW,
            lifetime=w.settings.auth.password_reset,clock=w.clock)
    status(w,target,actor,UserStatus.DISABLED)
    assert user(target).status=="disabled"
    with Session(w.runtime) as db:
        assert not reset.token_live(db,token.token_id,target.uid,NOW)
        if ts:assert not session_is_live(db,ts.session_id,now=NOW,idle=w.settings.auth.session_idle)
    status(w,target,actor,UserStatus.ACTIVE)
    after=user(target)
    assert after.status=="active" and after.status_safety=="proven"
    assert after.credential_operation_id==before.credential_operation_id
    with Session(w.runtime) as db:
        assert not reset.token_live(db,token.token_id,target.uid,NOW)
        assert db.get(AuthChangeReceipt,after.status_operation_id).reason_code=="admin_enable"


def test_enable_keeps_quarantine_and_initial_status_is_c3(world):
    w=world;actor=sign_in(w);target=add_other(w);quarantine(target)
    status(w,target,actor,UserStatus.DISABLED);status(w,target,actor,UserStatus.ACTIVE)
    assert user(target).credential_state=="quarantined"
    from types import SimpleNamespace
    from app.identity.passwords import hash_password
    with Session(w.engine) as db:
        u=User(email="initial@example.test",password_verifier=hash_password(SECRET));db.add(u);db.commit();uid=u.id
    with pytest.raises(ApiError,match="initial_status_verification_required"):
        status(w,SimpleNamespace(uid=uid),actor,UserStatus.ACTIVE)


@pytest.mark.parametrize("outcome",["revoked","expiry","csrf","not_admin"])
def test_status_rechecks_actor(world,outcome):
    w=world;actor=sign_in(w);target=add_other(w);clock=w.clock;csrf=None
    if outcome=="revoked":
        with Session(w.runtime) as db:
            revoke_session(db,session_id=actor.session_id,user_id=w.uid,reason=SessionRevokeReason.USER_REVOKED,now=NOW)
            commit_auth(db)
    elif outcome=="expiry":clock=lambda:NOW+w.settings.auth.session_idle
    elif outcome=="csrf":csrf="incorrect"
    else:
        w=add_other(w);actor=sign_in(w,email=f"{w.uid.hex}@example.test")
    with pytest.raises(ApiError):status(w,target,actor,UserStatus.DISABLED,clock=clock,csrf=csrf)
    assert user(target).status=="active"


@pytest.mark.parametrize("operation",["confirm","disable","enable","replace"])
@pytest.mark.parametrize("landed",[False,True])
def test_commit_lost_reply_never_success_or_delivery(world,monkeypatch,operation,landed):
    w=world;actor=sign_in(w);target=add_other(w);token=issue(w)
    if operation=="enable":status(w,target,actor,UserStatus.DISABLED)
    with Session(w.runtime) as db:
        commit=db.commit
        def uncertain():
            if landed:commit()
            else:db.rollback()
            raise OSError("synthetic lost commit reply")
        monkeypatch.setattr(db,"commit",uncertain)
        with pytest.raises(ApiError):
            if operation=="confirm":reset.confirm_reset(db,raw_token=token.token,new_password=NEW,now=NOW,policy=w.settings.password,clock=w.clock)
            elif operation=="replace":reset.request_reset(db,email="c1@example.test",now=NOW,lifetime=w.settings.auth.password_reset,clock=w.clock)
            else:set_user_status(db,user_id=target.uid,actor_id=w.uid,status=UserStatus.DISABLED if operation=="disable" else UserStatus.ACTIVE,
                now=NOW,actor_session_id=actor.session_id,csrf_token=actor.csrf_token,clock=w.clock)
    # Mixed auth rollback is reconciled by real recover, never a positive receipt from replay.
    run_recover(w)
    if operation=="confirm":
        assert verify_password(user(w).password_verifier,NEW)==landed
        assert user(w).credential_state==("usable" if landed else "quarantined")
    elif operation=="disable":assert user(target).status=="disabled"
    elif operation=="enable":assert user(target).status_safety==("proven" if landed else "held")
    else:
        with Session(w.runtime) as db:assert not reset.token_live(db,token.token_id,w.uid,NOW)


@pytest.mark.parametrize("shape",["none","torn","prefix","full"])
@pytest.mark.parametrize("operation",["confirm","disable","enable"])
def test_append_fault_whole_batch_unconfirmed_no_success(world,monkeypatch,shape,operation):
    w=world;actor=sign_in(w);target=add_other(w);token=issue(w)
    if operation=="enable":status(w,target,actor,UserStatus.DISABLED)
    real=journal.append
    def fail(path,state,records):
        if shape=="prefix":real(path,state,records[:1])
        elif shape=="torn":
            with path.open("ab") as f:f.write(records[0].line.encode()[:25]);f.flush()
        elif shape=="full":
            with monkeypatch.context() as fault:
                fault.setattr(journal.os,"fsync",lambda fd:(_ for _ in ()).throw(OSError("synthetic fsync")))
                real(path,state,records)
        raise OSError("synthetic append")
    monkeypatch.setattr(journal,"append",fail)
    with pytest.raises(ApiError,match="auth_journal_unavailable"):
        if operation=="confirm":confirm(w,token)
        else:status(w,target,actor,UserStatus.DISABLED if operation=="disable" else UserStatus.ACTIVE)
    with Session(w.engine) as db:
        assert db.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed"))>=1
    assert not recovery.evaluate_integrity(w.settings,w.runtime).open
    monkeypatch.setattr(journal,"append",real)
    run_recover(w,confirm_db_current=True)
    assert recovery.evaluate_integrity(w.settings,w.runtime).open


@pytest.mark.parametrize("query",["UPDATE password_reset_tokens SET used_at=now()", "INSERT INTO password_reset_tokens(user_id) VALUES(gen_random_uuid())",
    "UPDATE users SET status='disabled'", "UPDATE auth_change_receipts SET reason_code='admin_enable'"])
def test_runtime_has_no_raw_protected_write(world,query):
    with world.runtime.begin() as conn,pytest.raises(DBAPIError):conn.execute(text(query))


@pytest.mark.parametrize("outcome",["live","revoked","expired"])
def test_http_status_actor_race_after_real_dependency(world,monkeypatch,outcome):
    from fastapi import Depends, Request
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_db, get_clock, get_now, get_session_context
    from app.api.cookies import session_cookie_name
    w=world;actor=sign_in(w);target=add_other(w);at=[NOW]
    clock=lambda:at[0];entered=threading.Event();release=threading.Event()
    def pause(request: Request,db: Session=Depends(get_db),now=Depends(get_now),
              settings=Depends(get_settings),clock=Depends(get_clock)):
        ctx=get_session_context(request,db=db,now=now,settings=settings,clock=clock)
        assert not db.in_transaction();entered.set();assert release.wait(15);return ctx
    monkeypatch.setenv("RAS_ENVIRONMENT","development");monkeypatch.setenv("RAS_HTTP__COOKIE_SECURE","false")
    monkeypatch.setenv("RAS_HTTP__ALLOWED_ORIGINS",'["http://web.test"]')
    get_settings.cache_clear()
    app.dependency_overrides[get_clock]=lambda:clock
    app.dependency_overrides[get_now]=clock
    app.dependency_overrides[get_session_context]=pause
    try:
        with TestClient(app,base_url="http://web.test") as client,ThreadPoolExecutor(1) as pool:
            client.cookies.set(session_cookie_name(get_settings()),actor.token)
            client.cookies.set("ras_csrf",actor.csrf_token)
            job=pool.submit(client.post,f"/admin/users/{target.uid}/disable",
                headers={"origin":"http://web.test","x-csrf-token":actor.csrf_token})
            try:
                assert entered.wait(15)
                if outcome=="revoked":
                    with Session(w.runtime) as db:
                        revoke_session(db,session_id=actor.session_id,user_id=w.uid,reason=SessionRevokeReason.USER_REVOKED,now=NOW)
                        commit_auth(db)
                elif outcome=="expired":at[0]=NOW+w.settings.auth.session_idle
            finally:release.set()
            response=job.result(20)
            assert response.status_code==(200 if outcome=="live" else 401),response.text
            if outcome!="live":assert response.json()["error"]=="session_expired"
        assert user(target).status==("disabled" if outcome=="live" else "active")
    finally:app.dependency_overrides.clear();get_settings.cache_clear()


@pytest.mark.parametrize("outcome",["live","expired","replaced","disabled","hold"])
def test_delivery_final_token_guard_no_locks_during_io(world,outcome):
    from app.api.routes.auth import _send_quietly
    from app.delivery import OutgoingMessage, MessageKind
    w=world;token=issue(w);at=NOW
    if outcome=="expired":at=token.expires_at
    elif outcome=="replaced":issue(w)
    elif outcome=="disabled":transition(w,"status","disabled")
    elif outcome=="hold":recovery.hold(w.engine,w.settings,NOW)
    sent=[]
    class Adapter:
        def send(self,message,*,now):
            # Real writer can acquire User then exclusive journal while send runs.
            # A retained shared journal lock would time out and fail this test.
            with w.runtime.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout='1s'"))
                conn.execute(text("SELECT ras_auth_lock_users(:ids)"),{"ids":[w.uid]})
                conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":recovery.JOURNAL_LOCK})
            sent.append(message)
    message=OutgoingMessage(kind=MessageKind.PASSWORD_RESET,recipient=token.email,link="http://web.test/#synthetic",expires_at=token.expires_at)
    _send_quietly(Adapter(),message,w.runtime,token.token_id,w.uid,lambda:at)
    assert bool(sent)==(outcome=="live")


@pytest.mark.parametrize("operation",["confirm","disable","enable"])
@pytest.mark.parametrize("shape",["none","torn","prefix","full"])
@pytest.mark.parametrize("landed",[False,True])
def test_append_failure_and_uncertain_commit_matrix(world,monkeypatch,operation,shape,landed):
    w=world;actor=sign_in(w);target=add_other(w);token=issue(w)
    if operation=="enable":status(w,target,actor,UserStatus.DISABLED)
    real=journal.append
    def fail(path,state,records):
        if shape=="prefix":real(path,state,records[:1])
        elif shape=="torn":
            with path.open("ab") as f:f.write(records[0].line.encode()[:25]);f.flush()
        elif shape=="full":
            with monkeypatch.context() as fault:
                fault.setattr(journal.os,"fsync",lambda fd:(_ for _ in ()).throw(OSError("synthetic fsync")))
                real(path,state,records)
        raise OSError("synthetic append")
    monkeypatch.setattr(journal,"append",fail)
    with Session(w.runtime) as db:
        commit=db.commit
        def uncertain():
            if landed:commit()
            else:db.rollback()
            raise OSError("synthetic lost reply")
        monkeypatch.setattr(db,"commit",uncertain)
        with pytest.raises(ApiError):
            if operation=="confirm":reset.confirm_reset(db,raw_token=token.token,new_password=NEW,now=NOW,policy=w.settings.password,clock=w.clock)
            else:set_user_status(db,user_id=target.uid,actor_id=w.uid,status=UserStatus.DISABLED if operation=="disable" else UserStatus.ACTIVE,
                now=NOW,actor_session_id=actor.session_id,csrf_token=actor.csrf_token,clock=w.clock)
    monkeypatch.setattr(journal,"append",real)
    run_recover(w,confirm_db_current=landed)
    assert recovery.evaluate_integrity(w.settings,w.runtime).open
    if operation=="confirm":
        assert verify_password(user(w).password_verifier,NEW)==landed
        if not landed:assert user(w).credential_state==("usable" if shape in ("none","torn") else "quarantined")
    elif operation=="disable":assert user(target).status==("disabled" if landed or shape in ("prefix","full") else "active")
    elif not landed:assert user(target).status=="disabled"


def test_two_admins_disable_each_other_sorted_user_locks(world):
    w=world;other=add_other(w,role="admin")
    a=sign_in(w);b=sign_in(other,email=f"{other.uid.hex}@example.test")
    barrier=threading.Barrier(2)
    def attempt(actor,target,session):
        barrier.wait(10)
        try:status(actor,target,session,UserStatus.DISABLED);return "success"
        except ApiError as exc:
            assert exc.status_code==401
            return "denied"
    with ThreadPoolExecutor(2) as pool:
        jobs=[pool.submit(attempt,w,other,a),pool.submit(attempt,other,w,b)]
        assert sorted(j.result(20) for j in jobs)==["denied","success"]
    assert sum(user(x).status=="disabled" for x in (w,other))==1


def test_two_confirms_one_token_one_credential_transition(world,monkeypatch):
    w=world;token=issue(w);barrier=threading.Barrier(2);real=reset.hasher_for_policy
    start=journal.read(w.path).head.seq  # the fixture's own initial credential event is not counted
    class Paused:
        def __init__(self,policy):self.hasher=real(policy)
        def hash(self,password):
            result=self.hasher.hash(password);barrier.wait(15);return result
    monkeypatch.setattr(reset,"hasher_for_policy",Paused)
    def attempt():
        try:confirm(w,token);return "success"
        except InvalidToken:return "denied"
    with ThreadPoolExecutor(2) as pool:
        jobs=[pool.submit(attempt),pool.submit(attempt)]
        assert sorted(j.result(25) for j in jobs)==["denied","success"]
    records=journal.read(w.path).records
    assert len([r for r in records if r.seq>start and r.kind==journal.CREDENTIAL_TRANSITION and r.body["reason_code"]=="password_reset"])==1


@pytest.mark.parametrize("writer",["confirm","request","status"])
def test_final_guard_uses_clock_after_wait_for_journal(world,monkeypatch,writer):
    w=world;actor=sign_in(w);target=add_other(w);token=issue(w);at=[NOW]
    entered=threading.Event();release=threading.Event();real=auth_safety.require_final
    pid=[]
    # Pause before the real gate so the other connection can acquire journal.
    def pause(db,**kw):
        if kw.get("writer"):
            pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            entered.set();assert release.wait(15)
        return real(db,**kw)
    monkeypatch.setattr(auth_safety,"require_final",pause)
    def attempt():
        if writer=="confirm":return confirm(w,token,clock=lambda:at[0])
        if writer=="request":return issue(w,clock=lambda:at[0])
        return status(w,target,actor,UserStatus.DISABLED,clock=lambda:at[0])
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(attempt);assert entered.wait(15)
        with w.runtime.connect() as blocker:
            blocker.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":recovery.JOURNAL_LOCK})
            release.set()
            try:
                with w.engine.connect() as observer:
                    for _ in range(100):
                        event=observer.scalar(text("SELECT wait_event FROM pg_stat_activity WHERE pid=:pid"),{"pid":pid[0]})
                        if event=="advisory":break
                        observer.rollback();threading.Event().wait(.005)
                    else:raise AssertionError("writer did not wait for actual journal advisory lock")
                at[0]=NOW+w.settings.auth.password_reset
            finally:blocker.rollback()
        with pytest.raises(ApiError if writer=="status" else InvalidToken):job.result(20)
    assert user(target).status=="active"
    assert verify_password(user(w).password_verifier,SECRET)


@pytest.mark.parametrize("mode",["success","uncertain","quarantine","missing"])
def test_http_reset_request_confirm_delivery_outcome(world,monkeypatch,mode):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_clock,get_now
    from app.delivery import get_delivery
    w=world;sent=[]
    class Adapter:
        enabled=True
        def send(self,message,*,now):sent.append(message)
    monkeypatch.setenv("RAS_ENVIRONMENT","development");monkeypatch.setenv("RAS_HTTP__COOKIE_SECURE","false")
    monkeypatch.setenv("RAS_HTTP__ALLOWED_ORIGINS",'["http://web.test"]')
    monkeypatch.setenv("RAS_HTTP__PUBLIC_BASE_URL","http://web.test")
    get_settings.cache_clear()
    app.dependency_overrides[get_clock]=lambda:w.clock;app.dependency_overrides[get_now]=w.clock
    app.dependency_overrides[get_delivery]=lambda:Adapter()
    if mode=="quarantine":quarantine(w)
    if mode=="uncertain":
        real=Session.commit
        def lost(db):real(db);raise OSError("synthetic lost commit reply")
        monkeypatch.setattr(Session,"commit",lost)
    try:
        with TestClient(app,base_url="http://web.test") as client:
            assert client.get("/auth/csrf").status_code==200
            csrf=client.cookies.get("ras_csrf");headers={"origin":"http://web.test","x-csrf-token":csrf}
            response=client.post("/auth/password-reset/request",json={"email":"missing@example.test" if mode=="missing" else "c1@example.test"},headers=headers)
            assert response.status_code==202 and response.json()=={"status":"accepted"}
            assert len(sent)==(1 if mode=="success" else 0)
            if mode=="success":
                from urllib.parse import parse_qs,urlsplit
                raw=parse_qs(urlsplit(sent[0].link).fragment)["token"][0]
                response=client.post("/auth/password-reset/confirm",json={"token":raw,"new_password":NEW},headers=headers)
                assert response.status_code==200 and response.json()=={"status":"password_changed"}
                assert not any("ras_session=" in h and "Max-Age=0" not in h for h in response.headers.get_list("set-cookie"))
                assert client.post("/auth/password-reset/confirm",json={"token":raw,"new_password":NEW},headers=headers).status_code==400
    finally:app.dependency_overrides.clear();get_settings.cache_clear()


@pytest.mark.parametrize("damage",["wrong_owner","wrong_fence","null_fence","wrong_purpose"])
def test_sql_reset_create_and_live_fence_independent(world,damage):
    w=world;token=issue(w)
    with Session(w.runtime) as db:
        row=db.get(PasswordResetToken,token.token_id)
        assert not reset.token_live(db,token.token_id,uuid.uuid4(),NOW)
        uid=uuid.uuid4() if damage=="wrong_owner" else w.uid
        fence=None if damage=="null_fence" else uuid.uuid4() if damage=="wrong_fence" else row.credential_fence_operation_id
        purpose="quarantine" if damage=="wrong_purpose" else "normal"
        # Release the unique open slot using real closure + journal before testing INSERT.
        db.rollback()
        from app.identity.predicates import lock_users
        from app.identity.reset_tokens import revoke_open_tokens
        lock_users(db,[w.uid]);revoke_open_tokens(db,w.uid,reason="reset_replaced",now=NOW);commit_auth(db)
        with pytest.raises(DBAPIError):
            db.execute(text("SELECT ras_auth_token_create(:id,:uid,:fence,:purpose,:digest,:at,:expiry)"),
                       {"id":uuid.uuid4(),"uid":uid,"fence":fence,"purpose":purpose,"digest":"e"*64,
                        "at":NOW,"expiry":NOW+w.settings.auth.password_reset})
            db.commit()


@pytest.mark.parametrize("state",["disabled","held","latest_status_without_receipt"])
def test_quarantine_reset_requires_latest_proven_status_independently(world,state):
    w=world;quarantine(w)
    if state=="disabled":transition(w,"status","disabled")
    else:
        batch=append_changes(w,changes(w,credential=False),engine=w.runtime,rollback=True)
        if state=="held":run_recover(w)
        else:
            for entry in batch:recovery.replay_record(w.engine,entry.record,NOW)
    assert user(w).credential_state=="quarantined"
    assert issue(w,quarantine=True) is None
