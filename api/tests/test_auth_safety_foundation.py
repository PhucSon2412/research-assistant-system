"""C1 direct PostgreSQL/journal checks; HTTP pending is reported separately."""
import copy
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app import auth_safety, journal, recovery
from app.auth_safety import Change
from app.config import get_settings
from app.db_roles import provision
from app.identity.passwords import hash_password
from app.models import (User, UserSession, PasswordResetToken, Invitation, AuthChangeReceipt,
                        AuthSafetyHead, AuthSafetyState, RevocationJournalRecord, LibraryItem)
from tests.conftest import upgrade, alembic_config

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
SECRET = "C1-private-password-marker-123!"
LEGACY_SECRET = "C1-unverified-legacy-password-marker-456!"


@pytest.fixture
def foundation(engine, app_env, migrated_db_url, journal_path):
    app_env(migrated_db_url)
    with Session(engine) as db:
        u = User(id=uuid.uuid4(), email="c1@example.test", role="admin", password_verifier=hash_password(LEGACY_SECRET),
                 password_changed_at=NOW, created_at=NOW, updated_at=NOW)
        db.add(u)
        db.commit()
        uid = u.id
    return SimpleNamespace(engine=engine, uid=uid, path=journal_path, settings=get_settings())


def changes(w, *, credential=True, decision="active", at=NOW):
    # Generic runtime reason: C3 (0049) reserves verified_recovery for an operator case use.
    out = [Change(w.uid,"credential","password_reset",at,verifier=hash_password(SECRET),policy_version=1)] if credential else []
    if decision:
        out.append(Change(w.uid,"status","admin_enable" if decision == "active" else "admin_disable",at,decision=decision))
    return out


def append_changes(w, values=None, *, rollback=False, tokens=(), sessions=(), invitations=(), engine=None):
    with Session(engine or w.engine) as db:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"),{"ids":[w.uid]})
        batch = recovery.record_auth_endings(db,w.path,list(tokens),list(sessions),invitations=list(invitations),auth_changes=values if values is not None else changes(w))
        if rollback:
            db.rollback()
        else:
            db.commit()
        return batch


def run_recover(w, **kw):
    return recovery.recover(w.engine,SimpleNamespace(),w.settings,NOW,**kw)


def prove_initial(w, *, engine=None):
    """Legacy fixture: real credential write + an independent C3 status case."""
    runtime = engine or runtime_engine(w)
    try:
        from tests.test_c3_operator_recovery import operator_initial_status, operator_credential
        operator_credential(w,w.uid,password=SECRET)
        operator_initial_status(w, w.uid)
    finally:
        if engine is None:
            runtime.dispose()


def user(w):
    with Session(w.engine) as db:
        u = db.get(User,w.uid)
        db.expunge(u)
        return u


def event(kind=journal.CREDENTIAL_TRANSITION, uid=None):
    b={"user_id":str(uid or uuid.uuid4()),"operation_id":str(uuid.uuid4()),"generation":str(uuid.uuid4()),
       "reason_code":"verified_recovery","recorded_at":NOW.isoformat()}
    if kind==journal.ACCOUNT_STATUS_DECISION:
        b.update(decision="active",reason_code="admin_enable")
    return b


def raw_record(body, kind=journal.CREDENTIAL_TRANSITION):
    # Bypass Python intentionally: exercise independent SQL validation.
    e={"body":body,"seq":2,"kind":kind,"prev":"0"*64,"hash":"a"*64}
    return journal.canonical(e)


@pytest.mark.parametrize("kind",journal.AUTH_KINDS)
@pytest.mark.parametrize("shape",["missing","null","number","extra"])
@pytest.mark.parametrize("field",["user_id","operation_id","generation","reason_code","recorded_at"])
def test_python_and_sql_independently_reject_field_shapes(engine,kind,shape,field):
    b=event(kind)
    if shape=="missing": del b[field]
    elif shape=="null": b[field]=None
    elif shape=="number": b[field]=10
    else: b["password_verifier"]=SECRET
    with pytest.raises(journal.JournalError): journal.make_record(2,kind,b,journal.ZERO_HASH)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"),{"line":raw_record(b,kind)})


@pytest.mark.parametrize("kind",journal.AUTH_KINDS)
@pytest.mark.parametrize("field,value",[("operation_id","not-uuid"),pytest.param("generation","00000000-0000-1000-8000-000000000001",id="generation-uuid1"),("user_id","A"*36),
    ("reason_code","caller_rehash"),("recorded_at","2026-10-03T09:00:00"),("recorded_at","2026-10-03T24:00:00+00:00"),
    ("recorded_at","infinity"),("recorded_at","2026-02-30T09:00:00Z")])
def test_bad_values_no_sql_unknown(engine,kind,field,value):
    b=event(kind); b[field]=value
    with pytest.raises(journal.JournalError): journal.make_record(2,kind,b,journal.ZERO_HASH)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"),{"line":raw_record(b,kind)})


@pytest.mark.parametrize("decision",[None,123,"changed","enabled"])
def test_status_decision_parser_and_sql(engine,decision):
    b=event(journal.ACCOUNT_STATUS_DECISION); b["decision"]=decision
    with pytest.raises(journal.JournalError): journal.make_record(2,journal.ACCOUNT_STATUS_DECISION,b,journal.ZERO_HASH)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"),{"line":raw_record(b,journal.ACCOUNT_STATUS_DECISION)})


@pytest.mark.parametrize("column",["auth_user_id","auth_operation_id","auth_generation","auth_domain","auth_decision","auth_reason_code","auth_recorded_at"])
@pytest.mark.parametrize("null",[False,True])
def test_typed_mirror_binding_and_confirm_only(engine,column,null):
    b=event(); r=journal.make_record(2,journal.CREDENTIAL_TRANSITION,b,journal.ZERO_HASH)
    with Session(engine) as db:
        row=recovery._mirror(r,"replay","test",None,confirmed=False)
        actual=getattr(row,column)
        setattr(row,column,None if null else uuid.uuid4() if isinstance(actual,uuid.UUID) else NOW+timedelta(seconds=1) if isinstance(actual,datetime) else "bad")
        db.add(row)
        with pytest.raises(DBAPIError): db.flush()
        db.rollback()
    with Session(engine) as db:
        row=recovery._mirror(r,"replay","test",None,confirmed=False); db.add(row);db.flush()
        row.journal_confirmed=True;setattr(row,column,None)
        with pytest.raises(DBAPIError):db.flush()
        db.rollback()
    with Session(engine) as db:
        row=recovery._mirror(r,"replay","test",None,confirmed=False);db.add(row);db.flush()
        row.journal_confirmed=True;db.flush()  # allowed confirmation, with all typed fields unchanged
        with pytest.raises(DBAPIError):
            db.delete(row);db.flush()
        db.rollback()


def test_batch_valid_receipts_pending_and_secrets(foundation,caplog):
    w=foundation
    prove_initial(w)
    with w.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed"))==0
    u=user(w)
    assert (u.credential_state,u.status_safety)==("usable","proven")
    report=run_recover(w)
    assert report.reconciliation_complete and report.safety_ready and report.gate=="open"
    assert not recovery.evaluate(w.settings,w.engine).open
    assert recovery.evaluate_integrity(w.settings,w.engine).open
    with Session(w.engine) as db:
        assert len(list(db.scalars(select(AuthChangeReceipt))))==2
        auth_safety.verify(db,journal.read(w.path).records)
        audit=" ".join(db.execute(text("SELECT details::text FROM audit_events")).scalars())
        assert SECRET not in audit
    data=w.path.read_text()
    assert SECRET not in data and u.password_verifier not in data and "argon2" not in data
    assert SECRET not in caplog.text and u.password_verifier not in repr(changes(w))


@pytest.mark.parametrize("shape",["none","torn","prefix","full"])
@pytest.mark.parametrize("outcome",["commit","rollback","uncertain_commit","uncertain_rollback"])
def test_fault_matrix(foundation,monkeypatch,shape,outcome):
    w=foundation; prove_initial(w); original=journal.append
    def fail(path,state,records):
        assert deciding.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed"))==2
        assert deciding.scalar(text("SELECT count(*) FROM auth_change_receipts"))==4
        with Session(w.engine) as other:
            # The whole batch exists unconfirmed in the deciding transaction, not visible here.
            assert len(other.scalars(select(AuthChangeReceipt.operation_id)).all())==2
        if shape=="torn":
            with path.open("ab") as f:f.write(records[0].line[:30].encode())
        elif shape=="prefix":original(path,state,records[:1])
        elif shape=="full":
            # Fault at the actual fsync boundary, after _write flushed every line.
            with monkeypatch.context() as fault:
                def fail_sync(fd):raise OSError("synthetic_fsync")
                fault.setattr(journal.os,"fsync",fail_sync)
                original(path,state,records)
        raise OSError("synthetic_fsync")
    monkeypatch.setattr(journal,"append",fail)
    with Session(w.engine) as db:
        deciding=db
        db.scalar(select(User.id).where(User.id==w.uid).with_for_update(key_share=True))
        batch=recovery.record_auth_endings(db,w.path,[],[],auth_changes=changes(w))
        assert all(not r.written for r in batch)
        assert db.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed"))==2
        if outcome in ("commit","uncertain_commit"):
            db.commit()  # uncertain means the caller has no acknowledgment; no writer success is claimed
        else: db.rollback()
    assert not recovery.evaluate(w.settings,w.engine).open or (shape=="none" and outcome in ("rollback","uncertain_rollback"))
    monkeypatch.setattr(journal,"append",original)
    run_recover(w,confirm_db_current=outcome in ("commit","uncertain_commit"))
    u=user(w)
    if outcome in ("commit","uncertain_commit"):
        assert (u.credential_state,u.status_safety)==("usable","proven")
    else:
        # With a genuinely verified baseline, only complete new intents impose
        # restrictions. A torn/absent append cannot revoke the old permission.
        expected = {"none": ("usable", "proven"), "torn": ("usable", "proven"),
                    "prefix": ("quarantined", "proven"), "full": ("quarantined", "held")}[shape]
        assert (u.credential_state,u.status_safety)==expected
        with Session(w.engine) as db:assert len(db.scalars(select(AuthChangeReceipt.operation_id)).all())==2
    assert recovery.evaluate(w.settings,w.engine).reason=="worker_runtime_required"
    before=w.path.read_bytes();run_recover(w)
    assert user(w).credential_state==u.credential_state
    seqs=[r.seq for r in journal.read(w.path).records]
    assert seqs==list(range(1,len(seqs)+1))


def test_old_disable_does_not_destroy_new_enable_receipt(foundation):
    w=foundation;prove_initial(w)
    append_changes(w,changes(w,credential=False,decision="disabled",at=NOW+timedelta(seconds=1)))
    last=append_changes(w,changes(w,credential=False,decision="active",at=NOW+timedelta(seconds=2)))
    before=user(w)
    run_recover(w);run_recover(w)
    after=user(w)
    assert (after.status,after.status_safety,after.status_operation_id)==("active","proven",before.status_operation_id)
    assert after.status_operation_id==uuid.UUID(last[0].record.body["operation_id"])


@pytest.mark.parametrize("decision",["active","disabled"])
def test_rollback_replay_only_restricts_and_does_not_release(foundation,decision):
    w=foundation;append_changes(w)
    old=user(w)
    append_changes(w,changes(w,decision=decision,at=NOW+timedelta(seconds=1)),rollback=True)
    run_recover(w);run_recover(w)
    u=user(w)
    assert u.password_verifier==old.password_verifier and u.credential_generation==old.credential_generation
    assert u.credential_state=="quarantined"
    assert (u.status,u.status_safety)==(("disabled","proven") if decision=="disabled" else ("active","held"))
    with Session(w.engine) as db:assert len(list(db.scalars(select(AuthChangeReceipt))))==2


def test_absent_user_projection_replay_and_uuid_reuse(foundation,monkeypatch):
    w=foundation;state=journal.read(w.path);uid=uuid.uuid4()
    record=journal.next_record(state.head,journal.CREDENTIAL_TRANSITION,event(uid=uid))
    journal.append(w.path,state,[record])
    run_recover(w);run_recover(w)
    with Session(w.engine) as db:
        assert db.get(User,uid) is None and db.get(AuthSafetyHead,uid) is not None
        assert db.scalar(select(AuthChangeReceipt.operation_id)) is None
    op,gen=uuid.uuid4(),uuid.uuid4()
    sequence=iter([uuid.UUID(record.body["generation"]),uuid.UUID(record.body["operation_id"]),op,gen])
    monkeypatch.setattr(auth_safety.uuid,"uuid4",lambda:next(sequence))
    assert auth_safety.fresh_ids(journal.read(w.path).records)==(op,gen)


def test_collision_cross_domain_and_rollback_sql_and_file(foundation):
    w=foundation;batch=append_changes(w,rollback=True);r=batch[0].record
    state=journal.read(w.path);b=event(journal.ACCOUNT_STATUS_DECISION);b["generation"]=r.body["operation_id"]
    reused=journal.next_record(state.head,journal.ACCOUNT_STATUS_DECISION,b)
    with pytest.raises(journal.JournalError):journal.append(w.path,state,[reused])
    run_recover(w)
    with w.engine.begin() as conn,pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"),{"line":reused.line})


@pytest.mark.parametrize("step",["project","restrict","progress","verify"])
def test_recovery_fault_then_idempotent_present_records(foundation,monkeypatch,step):
    w=foundation;append_changes(w,rollback=True)
    original=Session.execute;original_verify=auth_safety.verify
    def fail(db,statement,*args,**kw):
        if "ras_auth_"+step+"(" in str(statement):raise RuntimeError("injected_step")
        return original(db,statement,*args,**kw)
    if step=="verify":monkeypatch.setattr(auth_safety,"verify",lambda *a:(_ for _ in ()).throw(RuntimeError("injected_verify")))
    else:monkeypatch.setattr(Session,"execute",fail)
    with pytest.raises(RuntimeError):run_recover(w)
    assert not recovery.evaluate(w.settings,w.engine).open
    monkeypatch.setattr(Session,"execute",original);monkeypatch.setattr(auth_safety,"verify",original_verify)
    run_recover(w);run_recover(w)
    with Session(w.engine) as db:auth_safety.verify(db,journal.read(w.path).records)


@pytest.mark.parametrize("revision",["0030","0031","0032"])
def test_upgrade_0032_data_hold_legacy_and_backfill(empty_db_url,journal_path,app_env,revision):
    upgrade(empty_db_url,revision);eng=create_engine(empty_db_url,hide_parameters=True)
    app_env(empty_db_url);settings=get_settings()
    # Construct a real 0032 journal registration, without invoking the 0033 ORM on an old DB.
    genesis=journal.genesis(uuid.uuid4(),NOW,"new_install")
    journal.create(journal_path,[genesis])
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO recovery_registrations(id,journal_id,genesis_hash,mode,registered_at) VALUES(1,:id,:hash,'new_install',:at)"),{"id":uuid.UUID(genesis.body["journal_id"]),"hash":genesis.hash,"at":NOW})
        conn.execute(text("INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed) VALUES(1,'genesis',:hash,:line,'genesis','new_install',true)"),{"hash":genesis.hash,"line":genesis.line})
    ids=[uuid.uuid4(),uuid.uuid4()]
    with eng.begin() as conn:
        for uid,status in zip(ids,["active","disabled"]):
            conn.execute(text("INSERT INTO users(id,email,role,status,password_verifier) VALUES(:u,:e,'admin',:s,:p)"),{"u":uid,"e":str(uid)+"@legacy.test","s":status,"p":hash_password(SECRET)})
        conn.execute(text("INSERT INTO sessions(user_id,token_hash,csrf_token_hash,created_at,last_activity_at,absolute_expires_at) VALUES(:u,:t,:t,:a,:a,:e)"),{"u":ids[0],"t":"a"*64,"a":NOW,"e":NOW+timedelta(hours=2)})
        conn.execute(text("INSERT INTO password_reset_tokens(user_id,token_hash,created_at,expires_at) VALUES(:u,:t,:a,:e)"),{"u":ids[0],"t":"b"*64,"a":NOW,"e":NOW+timedelta(hours=2)})
    recovery.hold(eng,settings,NOW)  # old snapshot has no receipt/projection/safety fields
    upgrade(empty_db_url)
    w=SimpleNamespace(engine=eng,path=journal_path,settings=settings)
    for _ in range(2):run_recover(w)
    with Session(eng) as db:
        active,disabled=[db.get(User,u) for u in ids]
        assert (active.credential_state,active.status_safety)==("quarantined","held")
        assert (disabled.credential_state,disabled.status,disabled.status_safety)==("quarantined","disabled","proven")
        assert db.scalar(select(AuthChangeReceipt.operation_id)) is None
        assert db.scalar(select(UserSession.revoked_reason))=="auth_safety_backfill"
        assert db.scalar(select(PasswordResetToken.closure_reason))=="auth_safety_backfill"
        assert db.get(AuthSafetyState,1).backfill_complete
    with pytest.raises(DBAPIError):command.downgrade(alembic_config(empty_db_url),"0032")
    eng.dispose()


def runtime_engine(w):
    # Login role passwords are disposable env values; never interpolate into logs.
    import psycopg
    from psycopg import sql
    for name,env in (("c1_runtime","RAS_C1_RUNTIME_PASSWORD"),("c1_recovery","RAS_C1_RECOVERY_PASSWORD")):
        with psycopg.connect(w.engine.url.render_as_string(hide_password=False).replace("postgresql+psycopg://","postgresql://"),autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(name,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS").format(sql.Identifier(name),sql.Literal(os.environ[env])))
    provision(w.engine,runtime="c1_runtime",recovery="c1_recovery",definer="c1_definer")
    eng=create_engine(w.engine.url.set(username="c1_runtime",password=os.environ["RAS_C1_RUNTIME_PASSWORD"]),hide_parameters=True)
    return eng


@pytest.fixture
def runtime(foundation):
    eng = runtime_engine(foundation)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.mark.parametrize("query",[
    "UPDATE users SET status='active'", "UPDATE users SET password_verifier='$argon2id$fake'", "UPDATE users SET credential_generation=gen_random_uuid()",
    "UPDATE auth_safety_heads SET credential_generation=gen_random_uuid()", "DELETE FROM auth_safety_heads", "DELETE FROM auth_change_receipts",
    "UPDATE auth_safety_state SET backfill_complete=true", "INSERT INTO auth_change_receipts(operation_id) VALUES(gen_random_uuid())",
    "ALTER TABLE users DISABLE TRIGGER ALL", "CREATE TABLE auth_bypass(id int)", "CREATE TEMP TABLE auth_bypass(id int)", "SET ROLE c1_migrator",
    "UPDATE revocation_journal_records SET journal_confirmed=true", "INSERT INTO sessions(user_id) VALUES(gen_random_uuid())",
    "UPDATE password_reset_tokens SET credential_fence_operation_id=gen_random_uuid()"])
def test_runtime_raw_mutation_and_fake_guc_denied(foundation,runtime,query):
    append_changes(foundation,engine=runtime)
    with runtime.begin() as conn,pytest.raises(DBAPIError):
        conn.execute(text("SET LOCAL ras.is_rehash='true'"));conn.execute(text(query))


def test_runtime_valid_routines_and_rehash_cas(foundation,runtime):
    w=foundation;prove_initial(w,engine=runtime);u=user(w);new=hash_password(SECRET)
    with foundation.engine.begin() as conn:
        conn.execute(text("INSERT INTO auth_hash_policies SELECT 2,memory_kib,time_cost,parallelism,salt_bytes,hash_bytes FROM auth_hash_policies WHERE version=1"))
    params={"uid":w.uid,"old":u.password_verifier,"gen":u.credential_generation,"op":u.credential_operation_id,"new":new,"policy":2,"at":NOW+timedelta(seconds=2)}
    stmt=text("SELECT ras_auth_rehash(:uid,:old,:gen,:op,:new,:policy,:at)")
    with runtime.begin() as conn:
        assert conn.scalar(stmt,params|{"op":uuid.uuid4()}) is False
        assert conn.scalar(stmt,params) is True
    after=user(w)
    assert after.credential_generation==u.credential_generation and after.password_changed_at==u.password_changed_at
    with runtime.begin() as conn,pytest.raises(DBAPIError):conn.execute(stmt,params|{"new":"plain-secret"})
    with runtime.begin() as conn,pytest.raises(DBAPIError):conn.execute(stmt,params|{"new":new.replace("m=65536","m=8")})
    with runtime.begin() as conn,pytest.raises(DBAPIError):conn.execute(stmt,params|{"policy":99})
    with runtime.begin() as conn:
        conn.execute(text("SELECT ras_auth_session_create(:id,:uid,:g,:t,:c,:a,:e)"),{"id":uuid.uuid4(),"uid":w.uid,"g":u.credential_generation,"t":"a"*64,"c":"b"*64,"a":NOW,"e":NOW+timedelta(hours=1)})
        conn.execute(text("SELECT ras_auth_token_create(:id,:uid,:f,'normal',:t,:a,:e)"),{"id":uuid.uuid4(),"uid":w.uid,"f":u.credential_operation_id,"t":"a"*64,"a":NOW,"e":NOW+timedelta(minutes=30)})
    with runtime.begin() as conn,pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_session_create(:id,:uid,:g,:t,:c,:a,:e)"),{"id":uuid.uuid4(),"uid":w.uid,"g":uuid.uuid4(),"t":"c"*64,"c":"d"*64,"a":NOW,"e":NOW+timedelta(hours=1)})
    with runtime.begin() as conn,pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_token_create(:id,:uid,:f,'normal',:t,:a,:e)"),{"id":uuid.uuid4(),"uid":w.uid,"f":uuid.uuid4(),"t":"b"*64,"a":NOW,"e":NOW+timedelta(minutes=30)})
    with runtime.connect() as conn:
        assert conn.scalar(text("SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname=current_user")) is False
        assert not conn.scalar(text("SELECT pg_has_role(current_user,'c1_definer','MEMBER')"))
        assert conn.scalar(text("SELECT pg_has_role(current_user,to_regrole('c1_migrator'),'MEMBER')")) is not True
        assert not conn.scalar(text("SELECT has_database_privilege(current_database(),'TEMP') OR has_database_privilege(current_database(),'CREATE')"))


@pytest.mark.parametrize("field",["user_id","domain","generation","reason_code","recorded_at","operation_id"])
def test_receipt_cross_binding_and_append_only(foundation,field):
    w=foundation;append_changes(w)
    with Session(w.engine) as db:
        r=db.scalar(select(AuthChangeReceipt).where(AuthChangeReceipt.domain=="credential"))
        values={c.name:getattr(r,c.name) for c in AuthChangeReceipt.__table__.columns}
        values["operation_id"]=uuid.uuid4()
        values[field]=uuid.uuid4() if field in ("operation_id","generation","user_id") else NOW+timedelta(seconds=1) if field=="recorded_at" else "bad"
        db.add(AuthChangeReceipt(**values))
        with pytest.raises(DBAPIError):db.flush()
        db.rollback()
    with w.engine.begin() as conn,pytest.raises(DBAPIError):conn.execute(text("DELETE FROM auth_change_receipts"))


@pytest.mark.parametrize("route",["/auth/csrf","/auth/login","/library/items","/files/00000000-0000-0000-0000-000000000000/original","/search","/rag","/admin/users","/collections"])
def test_http_pending_real_gate(foundation,route):
    from app.main import app
    w=foundation;run_recover(w)
    with TestClient(app) as client:
        response=client.get(route) if route not in ("/auth/login","/search","/rag") else client.post(route,json={})
        assert response.status_code==503 and response.json()=={"error":"service_recovering"}
        assert client.get("/healthz").status_code==200
        assert client.get("/readyz").status_code==503


def test_legacy_writers_empty_batch_and_bootstrap_blocked(foundation,runtime,app_env):
    from app.identity.session_commit import commit_auth
    from app.identity.bootstrap import bootstrap_admin
    from app.api.errors import ApiError
    w=foundation
    with Session(w.engine) as db,pytest.raises(ApiError):commit_auth(db)
    with pytest.raises(ApiError):bootstrap_admin(w.engine,email="other@example.test",password=SECRET,policy=w.settings.password)
    from app.ingest.worker import process_job
    app_env(runtime.url)  # the worker identity guard precedes the pending gate
    assert process_job(str(uuid.uuid4()))=="recovery_gate_closed"


def test_no_user_locks_after_journal_and_no_replay_release(foundation,runtime):
    w=foundation;append_changes(w,engine=runtime)
    with runtime.begin() as conn,pytest.raises(DBAPIError):
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),{"k":recovery.JOURNAL_LOCK})
        conn.execute(text("SELECT ras_auth_lock_users(:ids)"),{"ids":[w.uid]})
    with w.engine.begin() as conn:
        conn.execute(text("UPDATE users SET credential_state='quarantined',status_safety='held' WHERE id=:u"),{"u":w.uid})
    run_recover(w)
    assert (user(w).credential_state,user(w).status_safety)==("quarantined","held")
    with runtime.begin() as conn,pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_restrict(:u)"),{"u":w.uid})


def test_fixed_search_path_resists_temp_shadowing(foundation,runtime):
    w=foundation;u=user(w);op,gen=uuid.uuid4(),uuid.uuid4()
    with w.engine.begin() as conn:
        conn.execute(text("INSERT INTO auth_hash_policies SELECT 2,memory_kib,time_cost,parallelism,salt_bytes,hash_bytes FROM auth_hash_policies WHERE version=1"))
        # Adversarial fixture only: production provisioning denies TEMP. Prove fixed path
        # still works if deployment accidentally regrants it.
        database=conn.dialect.identifier_preparer.quote(conn.scalar(text("SELECT current_database()")))
        conn.execute(text(f"GRANT TEMPORARY ON DATABASE {database} TO c1_runtime"))
    with runtime.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE users AS SELECT * FROM public.users"))
        conn.execute(text("CREATE TEMP TABLE auth_safety_heads AS SELECT * FROM public.auth_safety_heads"))
        conn.execute(text("UPDATE pg_temp.users SET credential_state='usable',status_safety='proven',credential_generation=:g,credential_operation_id=:o,status_operation_id=:o,status_generation=:g"),{"g":gen,"o":op})
        conn.execute(text("INSERT INTO pg_temp.auth_safety_heads(user_id,projection_head_seq,credential_generation,credential_operation_id,status_operation_id,status_generation) VALUES(:u,2,:g,:o,:o,:g)"),{"u":w.uid,"g":gen,"o":op})
        assert conn.scalar(text("SELECT ras_auth_rehash(:u,:old,:g,:o,:new,2,:at)"),{"u":w.uid,"old":u.password_verifier,"g":gen,"o":op,"new":hash_password(SECRET),"at":NOW}) is False
    assert user(w).credential_state=="quarantined"


@pytest.mark.parametrize("landed",[True,False])
def test_commit_exception_never_becomes_acknowledgment(foundation,monkeypatch,landed):
    w=foundation;commit=Session.commit
    def uncertain(db):
        if landed:commit(db)
        else:db.rollback()
        raise OSError("lost_commit_reply")
    with Session(w.engine) as db:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"),{"ids":[w.uid]})
        batch=recovery.record_auth_endings(db,w.path,[],[],auth_changes=changes(w))
        assert all(r.written for r in batch)
        monkeypatch.setattr(Session,"commit",uncertain)
        with pytest.raises(OSError):db.commit()
        db.rollback()
    monkeypatch.setattr(Session,"commit",commit)
    run_recover(w)
    assert user(w).credential_state==("usable" if landed else "quarantined")


@pytest.mark.parametrize("registered",[False,True])
def test_registration_cannot_prove_legacy(empty_db_url,journal_path,app_env,registered):
    upgrade(empty_db_url);eng=create_engine(empty_db_url,hide_parameters=True);app_env(empty_db_url)
    with Session(eng) as db:
        u=User(email="legacy@example.test",password_verifier=hash_password(SECRET));db.add(u);db.commit();uid=u.id
    settings=get_settings()
    with pytest.raises(recovery.RecoveryError,match="database_not_new"):recovery.init_new(eng,settings,NOW)
    recovery.register_existing(eng,settings,NOW)
    recovery.recover(eng,SimpleNamespace(),settings,NOW,confirm_db_current=registered)
    with Session(eng) as db:
        u=db.get(User,uid);assert (u.credential_state,u.status_safety)==("quarantined","held")
        assert db.scalar(select(AuthChangeReceipt.operation_id)) is None
    eng.dispose()


def test_old_domains_direct_closure_positive_and_reopen_negative(foundation):
    from app.identity.invitation_endings import lock_email
    w=foundation;prove_initial(w);u=user(w)
    sid,tid,iid=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    with Session(w.engine) as db:
        db.add(UserSession(id=sid,user_id=w.uid,credential_generation=u.credential_generation,token_hash="c"*64,
            csrf_token_hash="d"*64,created_at=NOW,last_activity_at=NOW,absolute_expires_at=NOW+timedelta(hours=2)))
        db.add(PasswordResetToken(id=tid,user_id=w.uid,credential_fence_operation_id=u.credential_operation_id,
            token_hash="d"*64,created_at=NOW,expires_at=NOW+timedelta(minutes=30)))
        db.add(Invitation(id=iid,email="invite@example.test",token_hash="e"*64,created_at=NOW,expires_at=NOW+timedelta(days=3),invited_by_user_id=w.uid))
        db.commit()
    with Session(w.engine) as db:
        lock_email(db,"invite@example.test")
        db.execute(text("SELECT ras_auth_lock_users(:ids)"),{"ids":[w.uid]})
        token=db.scalar(select(PasswordResetToken).where(PasswordResetToken.id==tid).with_for_update())
        session=db.scalar(select(UserSession).where(UserSession.id==sid).with_for_update())
        invitation=db.scalar(select(Invitation).where(Invitation.id==iid).with_for_update())
        token.revoked_at,token.closure_reason=NOW+timedelta(seconds=1),"user_disabled"
        session.revoked_at,session.revoked_reason=NOW+timedelta(seconds=1),"user_disabled"
        invitation.revoked_at,invitation.closure_reason=NOW+timedelta(seconds=1),"admin_revoked"
        db.flush()
        batch=recovery.record_auth_endings(db,w.path,[token],[session],invitations=[invitation],auth_changes=changes(w,at=NOW+timedelta(seconds=1),decision="disabled"))
        assert [r.record.kind for r in batch]==[*journal.AUTH_KINDS,journal.INVITATION_CLOSURE,journal.RESET_TOKEN_CLOSURE,journal.SESSION_REVOCATION]
        db.commit()
    for table,field,identifier in (("sessions","revoked_at",sid),("password_reset_tokens","revoked_at",tid),("invitations","revoked_at",iid)):
        with w.engine.begin() as conn,pytest.raises(DBAPIError):
            conn.execute(text(f"UPDATE {table} SET {field}=NULL WHERE id=:id"),{"id":identifier})
    run_recover(w);run_recover(w)
    with Session(w.engine) as db:
        assert db.get(UserSession,sid).revoked_reason=="user_disabled"
        assert db.get(PasswordResetToken,tid).closure_reason=="user_disabled"
        assert db.get(Invitation,iid).closure_reason=="admin_revoked"


def test_common_private_acl_and_review_closure_direct(foundation):
    from app.authz import Principal, visible_items_clause, m2a_private_derivatives_clause, review_grant_clause
    from app.models import PaperVersion, SourceFile, ReviewRequest
    from app.review_closing import commit_close
    w=foundation;owner=uuid.uuid4();other=uuid.uuid4();item_id=uuid.uuid4();common=uuid.uuid4()
    with Session(w.engine) as db:
        db.add_all([User(id=owner,email="owner@example.test",password_verifier=hash_password(SECRET)),
                    User(id=other,email="other@example.test",password_verifier=hash_password(SECRET))]);db.flush()
        db.add_all([LibraryItem(id=item_id,visibility="private",owner_user_id=owner),LibraryItem(id=common,visibility="common",publication_state="listed")]);db.flush()
        version=PaperVersion(item_id=item_id);db.add(version);db.flush()
        file=SourceFile(item_id=item_id,version_id=version.id,storage_key="originals/"+uuid.uuid4().hex,
                        verified_mime="text/markdown",byte_size=100,checksum_sha256="a"*64);db.add(file);db.flush()
        rr=ReviewRequest(item_id=item_id,version_id=version.id,source_file_id=file.id,requester_user_id=owner,
                         status="open",file_checksum_sha256="a"*64,rights_epoch_at_submit=0,notice_version="n",
                         declared_source="source",declared_rights="rights",submitted_at=NOW);db.add(rr);db.commit();rid=rr.id
    admin=Principal(w.uid,"admin",uuid.uuid4(),"admin@example.test",None)
    p_owner=Principal(owner,"user",uuid.uuid4(),"owner@example.test",None)
    p_other=Principal(other,"user",uuid.uuid4(),"other@example.test",None)
    with Session(w.engine) as db:
        assert set(db.scalars(select(LibraryItem.id).where(visible_items_clause(p_owner))))=={item_id,common}
        assert set(db.scalars(select(LibraryItem.id).where(visible_items_clause(p_other))))=={common}
        assert set(db.scalars(select(LibraryItem.id).where(m2a_private_derivatives_clause(p_owner))))=={item_id}
        assert list(db.scalars(select(LibraryItem.id).where(m2a_private_derivatives_clause(p_other))))==[]
        assert db.scalar(select(SourceFile.id).join(PaperVersion,PaperVersion.id==SourceFile.version_id).join(LibraryItem,LibraryItem.id==SourceFile.item_id)
                         .where(review_grant_clause(admin))) is not None
    with Session(w.engine) as db,pytest.raises(DBAPIError):
        rr=db.get(ReviewRequest,rid);rr.status="withdrawn";rr.closed_at=NOW;rr.closed_by_user_id=owner;rr.close_reason_code="withdrawn_by_requester";db.commit()
    with Session(w.engine) as db:
        rr=db.scalar(select(ReviewRequest).where(ReviewRequest.id==rid).with_for_update())
        rr.status="withdrawn";rr.closed_at=NOW;rr.closed_by_user_id=owner;rr.close_reason_code="withdrawn_by_requester"
        commit_close(db,rr,journal_path=w.path)
    run_recover(w)
    with Session(w.engine) as db:
        assert db.scalar(select(SourceFile.id).join(PaperVersion,PaperVersion.id==SourceFile.version_id).join(LibraryItem,LibraryItem.id==SourceFile.item_id)
                         .where(review_grant_clause(admin))) is None


def test_recovery_login_role_restricts_without_receipts(foundation,runtime):
    w=foundation;append_changes(w,engine=runtime,rollback=True)
    eng=create_engine(w.engine.url.set(username="c1_recovery",password=os.environ["RAS_C1_RECOVERY_PASSWORD"]),hide_parameters=True)
    try:
        r=recovery.recover(eng,SimpleNamespace(),w.settings,NOW)
        assert r.reconciliation_complete and r.safety_ready and r.gate=="open"
        with Session(eng) as db:
            assert db.get(User,w.uid).credential_state=="quarantined"
            assert db.scalar(select(AuthChangeReceipt.operation_id)) is None
            assert db.get(AuthSafetyState,1).backfill_complete
        for sql in ("UPDATE users SET status='active'", "SELECT ras_auth_change(:u,:o,NULL,NULL)",
                    "SELECT ras_auth_rehash(:u,'old',:o,:o,'new',1,:at)"):
            with eng.begin() as conn,pytest.raises(DBAPIError):
                conn.execute(text(sql),{"u":w.uid,"o":uuid.uuid4(),"at":NOW})
    finally:eng.dispose()


def test_quarantine_token_allowed_but_normal_token_denied(foundation,runtime):
    w=foundation;prove_initial(w,engine=runtime)
    append_changes(w,changes(w,decision=None,at=NOW+timedelta(seconds=1)),engine=runtime,rollback=True)
    run_recover(w)
    with Session(w.engine) as db:fence=db.get(AuthSafetyHead,w.uid).credential_operation_id
    query=text("SELECT ras_auth_token_create(:id,:u,:f,:purpose,:t,:at,:expires)")
    params={"id":uuid.uuid4(),"u":w.uid,"f":fence,"purpose":"quarantine","t":"f"*64,"at":NOW,"expires":NOW+timedelta(minutes=20)}
    with runtime.begin() as conn:conn.execute(query,params)
    with runtime.begin() as conn,pytest.raises(DBAPIError):conn.execute(query,params|{"id":uuid.uuid4(),"purpose":"normal","t":"e"*64})


@pytest.mark.parametrize("purpose", ["normal", "quarantine"])
@pytest.mark.parametrize("decision", ["disabled", "active"])
def test_reset_token_rejects_status_head_ahead_of_snapshot(foundation, runtime, purpose, decision):
    w = foundation
    append_changes(w, engine=runtime)
    if purpose == "quarantine":
        with w.engine.begin() as conn:
            conn.execute(text("UPDATE users SET credential_state='quarantined' WHERE id=:u"), {"u": w.uid})
    before = user(w)
    batch = append_changes(w, changes(w, credential=False, decision=decision, at=NOW+timedelta(seconds=1)),
                           engine=runtime, rollback=True)
    # First replay phase only: preserve the snapshot while advancing mirror/projection.
    recovery.replay_record(w.engine, batch[0].record, NOW)
    with Session(w.engine) as db:
        current, head = db.get(User, w.uid), db.get(AuthSafetyHead, w.uid)
        assert (current.status, current.status_safety) == ("active", "proven")
        assert (current.status_operation_id, current.status_generation) == (before.status_operation_id, before.status_generation)
        assert head.status_decision == decision and head.status_operation_id != current.status_operation_id
        assert db.get(AuthChangeReceipt, head.status_operation_id) is None
        fence = head.credential_operation_id
    token_id = uuid.uuid4()
    with pytest.raises(DBAPIError, match="reset token credential fence mismatch"), runtime.begin() as conn:
        conn.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [w.uid]})
        conn.execute(text("SELECT ras_auth_token_create(:id,:u,:f,:purpose,:t,:at,:expires)"),
                     {"id": token_id, "u": w.uid, "f": fence, "purpose": purpose, "t": "a"*64,
                      "at": NOW, "expires": NOW+timedelta(minutes=20)})
    with Session(w.engine) as db:
        assert db.get(PasswordResetToken, token_id) is None
        assert db.get(AuthSafetyState, 1).cutover_state == "pending"


@pytest.mark.parametrize("purpose", ["normal", "quarantine"])
def test_reset_token_current_status_receipt_allowed_with_credential_fence(foundation, runtime, purpose):
    w = foundation
    prove_initial(w, engine=runtime)
    if purpose == "quarantine":
        with w.engine.begin() as conn:
            conn.execute(text("UPDATE users SET credential_state='quarantined' WHERE id=:u"), {"u": w.uid})
    with Session(w.engine) as db:
        current, head = db.get(User, w.uid), db.get(AuthSafetyHead, w.uid)
        receipt = db.get(AuthChangeReceipt, head.status_operation_id)
        assert (current.status_operation_id, current.status_generation) == (head.status_operation_id, head.status_generation)
        assert (receipt.user_id, receipt.domain, receipt.generation, receipt.decision) == (w.uid, "status", head.status_generation, "active")
        fence = head.credential_operation_id
    query = text("SELECT ras_auth_token_create(:id,:u,:f,:purpose,:t,:at,:expires)")
    params = {"id": uuid.uuid4(), "u": w.uid, "f": fence, "purpose": purpose, "t": "b"*64,
              "at": NOW, "expires": NOW+timedelta(minutes=20)}
    with runtime.begin() as conn:
        conn.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [w.uid]})
        conn.execute(query, params)
    with Session(w.engine) as db:
        token = db.get(PasswordResetToken, params["id"])
        assert (token.credential_fence_operation_id, token.recovery_purpose) == (fence, purpose)
    for invalid in ({"f": uuid.uuid4()}, {"purpose": "quarantine" if purpose == "normal" else "normal"}):
        with pytest.raises(DBAPIError, match="reset token credential fence mismatch"), runtime.begin() as conn:
            conn.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [w.uid]})
            conn.execute(query, params | {"id": uuid.uuid4(), "t": "c"*64} | invalid)


def test_0034_upgrade_preserves_auth_history_roles_and_pending(empty_db_url, journal_path, app_env):
    upgrade(empty_db_url, "0033")
    owner = create_engine(empty_db_url, hide_parameters=True)
    app_env(empty_db_url)
    settings = get_settings()
    recovery.init_new(owner, settings, NOW)
    uid = uuid.uuid4()
    with Session(owner) as db:
        db.add(User(id=uid, email="upgrade@example.test", password_verifier=hash_password(SECRET)))
        db.commit()
    w = SimpleNamespace(engine=owner, uid=uid, path=journal_path, settings=settings)
    rt = runtime_engine(w)
    try:
        append_changes(w, engine=rt)
        snapshot_queries = (
            "SELECT * FROM auth_change_receipts ORDER BY operation_id",
            "SELECT * FROM auth_safety_heads ORDER BY user_id",
            "SELECT * FROM auth_safety_state",
            "SELECT * FROM users ORDER BY id",
            "SELECT oid,proowner,proacl,proconfig,prosecdef FROM pg_proc WHERE oid='ras_auth_token_fence()'::regprocedure",
            "SELECT oid,proowner,proacl,proconfig,prosecdef FROM pg_proc WHERE proname='ras_auth_token_create'",
        )
        with owner.connect() as conn:
            before = [conn.execute(text(q)).all() for q in snapshot_queries]
        journal_before = journal_path.read_bytes()
        upgrade(empty_db_url, "0034")
        upgrade(empty_db_url, "0034")
        with owner.connect() as conn:
            assert [conn.execute(text(q)).all() for q in snapshot_queries] == before
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0034"
        assert journal_path.read_bytes() == journal_before
        current = user(w)
        query = text("SELECT ras_auth_token_create(:id,:u,:f,:purpose,:t,:at,:expires)")
        for purpose in ("normal", "quarantine"):
            if purpose == "quarantine":
                with owner.begin() as conn:
                    conn.execute(text("UPDATE users SET credential_state='quarantined' WHERE id=:u"), {"u": uid})
            with Session(rt) as db:
                db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
                db.execute(query, {"id": uuid.uuid4(), "u": uid, "f": current.credential_operation_id,
                             "purpose": purpose, "t": ("a" if purpose == "normal" else "b")*64,
                             "at": NOW, "expires": NOW+timedelta(minutes=20)})
                assert db.scalar(select(PasswordResetToken.id)) is not None
                db.rollback()  # Keep the one-open-token-per-User contract for the next purpose.
        with pytest.raises(DBAPIError, match="auth safety history prevents downgrade"):
            command.downgrade(alembic_config(empty_db_url), "0033")
        assert recovery.evaluate(settings, owner).reason == "worker_runtime_required"
    finally:
        rt.dispose()
        owner.dispose()


def test_0034_pristine_downgrade_restores_0033_guard(empty_db_url):
    upgrade(empty_db_url, "0033")
    eng = create_engine(empty_db_url, hide_parameters=True)
    try:
        with eng.connect() as conn:
            original = conn.scalar(text("SELECT pg_get_functiondef('ras_auth_token_fence()'::regprocedure)"))
        upgrade(empty_db_url)
        command.downgrade(alembic_config(empty_db_url), "0033")
        with eng.connect() as conn:
            assert conn.scalar(text("SELECT pg_get_functiondef('ras_auth_token_fence()'::regprocedure)")) == original
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0033"
        upgrade(empty_db_url)
    finally:
        eng.dispose()


def test_secret_field_and_scalar_errors_are_redacted(engine):
    b=event();b["operation_id"]=SECRET
    with engine.begin() as conn,pytest.raises(DBAPIError) as err:
        conn.execute(text("SELECT ras_auth_mirror(:line,'replay','test',false)"),{"line":raw_record(b)})
    assert SECRET not in str(err.value.orig)


def test_common_publish_download_revoke_cleanup_direct(engine, app_env, journal_path, monkeypatch):
    """Runtime account/approval/publish, then labelled legacy owner revoke/cleanup."""
    from app import publication
    from app.authz import Principal
    from app.common_original import common_original_rows
    from app.identity.bootstrap import bootstrap_admin
    from app.identity.login import login
    from app.models import SourceFile, DeletionTombstone
    from app.models.enums import RevocationReason
    from app.api.errors import ApiError
    from tests.resource_fixtures import common_copy, runtime_publish, BODY, SHA
    app_env(engine.url)
    w = SimpleNamespace(engine=engine, path=journal_path, settings=get_settings())
    w.runtime = runtime_engine(w)
    app_env(w.runtime.url)
    w.settings = get_settings()
    monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
    try:
        w.uid = bootstrap_admin(w.runtime, email='common-admin@example.test', password=SECRET,
                                policy=w.settings.password, clock=lambda: NOW).user_id
        with Session(w.runtime) as db:
            w.actor = login(db, email='common-admin@example.test', password=SECRET, now=NOW,
                            settings=w.settings, clock=lambda: NOW)[1]
        copy = common_copy(w, download=True, search=False, indexed=False, pending=True)
        admin = Principal(w.uid, 'admin', w.actor.session_id, 'common-admin@example.test', None)
        with Session(w.runtime) as db:
            assert db.execute(common_original_rows(admin)).first() is None
        with pytest.raises(ApiError, match='checksum_confirmation_mismatch'):
            runtime_publish(w, copy.item, copy.storage, checksum='a'*64)
        runtime_publish(w, copy.item, copy.storage)
        with Session(w.runtime) as db:
            assert db.execute(common_original_rows(admin)).first()[0].id == copy.file
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text("UPDATE library_items SET publication_state='revoked',rights_epoch=rights_epoch+1 WHERE id=:i"),
                      {'i': copy.item})
        with Session(engine) as db:  # legacy journaled revoke adversary; no runtime certification
            publication.revoke(db, admin, copy.item, RevocationReason.RIGHTS_RECORDED_IN_ERROR,
                               NOW, timedelta(seconds=1), journal_path=w.path)
        with Session(w.runtime) as db:
            assert db.execute(common_original_rows(admin)).first() is None
        # Memory storage implements the legacy cleanup HEAD contract only here.
        class Storage:
            def size(self, key):
                from botocore.exceptions import ClientError
                if key not in copy.storage.objects:
                    raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'HeadObject')
                return len(copy.storage.objects[key])
            def delete(self, key):
                copy.storage.delete(key)
        counts = publication.operator_cleanup_revoked(engine, Storage(), NOW+timedelta(seconds=2), 100)
        assert not any(k.startswith('error:') for k in counts), counts
        assert publication.operator_cleanup_revoked(engine, Storage(), NOW+timedelta(seconds=2), 100) == {}
        assert copy.key not in copy.storage.objects and list(copy.storage.objects.values()) == [BODY]
        with Session(engine) as db:
            assert db.get(LibraryItem, copy.item).title is None
            assert db.scalar(select(DeletionTombstone.purged_at)) == NOW+timedelta(seconds=2)
    finally:
        w.runtime.dispose()
