"""C4 real operational readiness, with runtime login and no gate replacement."""
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from app import auth_safety, journal, recovery
from app.api.errors import ApiError
from app.clock import utcnow
from app.config import get_settings
from app.main import app
from app.storage import get_storage


def test_ready_mutations_remain_ready(api):
    api.bootstrap_admin()
    uid = api.create_user('c4-user@example.org')
    admin = api.admin_browser()
    assert TestClient(app).get('/readyz').status_code == 200
    response = admin.post(f'/admin/users/{uid}/disable')
    assert response.status_code == 200
    assert TestClient(app).get('/readyz').status_code == 200
    assert admin.post(f'/admin/users/{uid}/enable').status_code == 200
    assert TestClient(app).get('/readyz').status_code == 200
    assert admin.post('/auth/logout').status_code == 204
    assert TestClient(app).get('/readyz').status_code == 200


def test_owner_identity_cannot_serve_even_with_valid_contract(api, app_env):
    app_env(api.setup_engine.url)
    assert recovery.evaluate(get_settings(), api.setup_engine).reason == 'worker_runtime_required'
    assert TestClient(app).get('/readyz').status_code == 503
    assert TestClient(app).get('/library/items').status_code == 503


@pytest.mark.parametrize('fault', ['replication','checkpoint_column','registration_column',
                                 'operator_map_column','operator_case_column'])
def test_runtime_role_privilege_drift_closes_gate(api, app_env, fault):
    """A distinct synthetic LOGIN avoids changing the role shared by other test DBs."""
    import secrets
    import uuid
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from app.db_roles import provision
    name, password = 'c4_rt_' + uuid.uuid4().hex, secrets.token_urlsafe(32)
    owner_url = api.setup_engine.url
    with psycopg.connect(owner_url.render_as_string(hide_password=False).replace(
            'postgresql+psycopg://','postgresql://')) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOINHERIT').format(
            sql.Identifier(name),sql.Literal(password)))
    runtime = create_engine(owner_url.set(username=name,password=password),hide_parameters=True)
    try:
        provision(api.setup_engine,runtime=name,recovery='c1_recovery',definer='c1_definer')
        assert recovery.evaluate(get_settings(),runtime).open
        with api.setup_engine.begin() as conn:
            query = {
                'replication': f'ALTER ROLE "{name}" REPLICATION',
                'checkpoint_column': f'GRANT UPDATE(verified_hash) ON auth_operational_state TO "{name}"',
                'registration_column': f'GRANT UPDATE(genesis_hash) ON recovery_registrations TO "{name}"',
                'operator_map_column': f'GRANT INSERT(login_role) ON auth_recovery_operators TO "{name}"',
                'operator_case_column': f'GRANT UPDATE(expires_at) ON auth_recovery_cases TO "{name}"',
            }[fault]
            conn.execute(text(query))
        app_env(runtime.url)
        assert recovery.evaluate(get_settings(),runtime).reason == 'worker_runtime_required'
        assert TestClient(app).get('/readyz').status_code == 503
        assert TestClient(app).get('/library/items').status_code == 503
        if fault != 'replication':
            provision(api.setup_engine,runtime=name,recovery='c1_recovery',definer='c1_definer')
            assert recovery.evaluate(get_settings(),runtime).open
            assert TestClient(app).get('/readyz').status_code == 200
    finally:
        runtime.dispose()
        with api.setup_engine.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{name}"'))
            conn.execute(text(f'DROP ROLE "{name}"'))


@pytest.mark.parametrize('fault', ['replication','membership','table_owner','schema_owner'])
def test_privileged_definer_closes_contract_and_cannot_be_reprovisioned(api, fault):
    import uuid
    from app.db_roles import provision
    name = 'c4_df_' + uuid.uuid4().hex
    parent = 'c4_parent_' + uuid.uuid4().hex
    with api.setup_engine.begin() as conn:
        conn.execute(text(f'CREATE ROLE "{name}" NOLOGIN'))
    try:
        provision(api.setup_engine,runtime='c1_runtime',recovery='c1_recovery',definer=name)
        assert TestClient(app).get('/readyz').status_code == 200
        with api.setup_engine.begin() as conn:
            if fault == 'replication':
                conn.execute(text(f'ALTER ROLE "{name}" REPLICATION'))
            elif fault == 'membership':
                conn.execute(text(f'CREATE ROLE "{parent}" NOLOGIN'))
                conn.execute(text(f'GRANT "{parent}" TO "{name}"'))
            elif fault == 'table_owner':
                conn.execute(text('CREATE TABLE c4_owned(id int)'))
                conn.execute(text(f'ALTER TABLE c4_owned OWNER TO "{name}"'))
            else:
                conn.execute(text(f'CREATE SCHEMA c4_owned AUTHORIZATION "{name}"'))
        assert recovery.evaluate(get_settings(),api.engine).reason == 'auth_safety_not_provisioned'
        assert TestClient(app).get('/readyz').status_code == 503
        with pytest.raises(ValueError,match='definer'):
            provision(api.setup_engine,runtime='c1_runtime',recovery='c1_recovery',definer=name)
    finally:
        provision(api.setup_engine,runtime='c1_runtime',recovery='c1_recovery',definer='c1_definer')
        with api.setup_engine.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{name}"'))
            conn.execute(text(f'DROP ROLE "{name}"'))
            if fault == 'membership':
                conn.execute(text(f'DROP ROLE "{parent}"'))


def test_hold_wins_after_entry_and_recover_restores_readiness(api):
    settings = get_settings()
    assert recovery.evaluate(settings, api.engine).open
    recovery.hold(api.setup_engine, settings, utcnow())
    with Session(api.engine) as db, pytest.raises(ApiError) as failure:
        auth_safety.require_final(db)
    assert failure.value.status_code == 503
    assert TestClient(app).get('/readyz').status_code == 503
    report = recovery.recover(api.setup_engine, get_storage(settings), settings, utcnow())
    assert report.safety_ready and report.reconciliation_complete
    assert TestClient(app).get('/readyz').status_code == 200


@pytest.mark.parametrize('fault', ['checkpoint','projection','journal_missing','journal_torn'])
def test_live_postcondition_corruption_closes_gate(api, fault):
    api.bootstrap_admin()
    settings = get_settings()
    assert TestClient(app).get('/readyz').status_code == 200
    if fault == 'checkpoint':
        with api.setup_engine.begin() as conn:
            conn.execute(text("UPDATE auth_operational_state SET verified_hash=repeat('0',64)"))
    elif fault == 'projection':
        with api.setup_engine.begin() as conn:
            # Privileged corruption setup only; the trigger is restored before
            # the unmodified runtime/HTTP gate is exercised.
            conn.execute(text('ALTER TABLE auth_safety_heads DISABLE TRIGGER trg_auth_head_final'))
            conn.execute(text('DELETE FROM auth_safety_heads'))
            conn.execute(text('ALTER TABLE auth_safety_heads ENABLE TRIGGER trg_auth_head_final'))
    elif fault == 'journal_missing':
        settings.recovery.journal_path.unlink()
    else:
        with settings.recovery.journal_path.open('ab') as out:
            out.write(b'{torn')
    assert TestClient(app).get('/healthz').status_code == 200
    assert TestClient(app).get('/readyz').status_code == 503
    assert TestClient(app).get('/library/items').status_code == 503


@pytest.mark.parametrize('query', [
    'UPDATE auth_operational_state SET verified_seq=1',
    'DELETE FROM auth_operational_state',
    'DELETE FROM recovery_registrations',
    "SELECT ras_auth_complete_contract(1,repeat('0',64))",
    'SELECT ras_auth_operator_whoami()',
    'CREATE TEMP TABLE bypass(id int)',
])
def test_runtime_cannot_complete_or_bypass_contract(api, query):
    from sqlalchemy.exc import DBAPIError
    with api.engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text(query))


def test_0050_upgrade_preserves_proven_account_and_keeps_legacy_account_held(
        empty_db_url, journal_path, app_env, monkeypatch):
    """Actual 0050 schema, C3 receipts, hold before migration, then reprovision/recover."""
    import uuid
    from types import SimpleNamespace
    from app.db import make_engine
    from app.models import User
    from app.identity.passwords import hash_password
    from tests.conftest import upgrade, register_new_install
    from tests.helpers import DEV_ENV, Browser
    from tests.test_auth_safety_foundation import runtime_engine, NOW, SECRET, LEGACY_SECRET
    from tests.test_c3_operator_recovery import operator_credential, operator_initial_status
    upgrade(empty_db_url,'0050')
    register_new_install(empty_db_url,journal_path)
    for key,value in DEV_ENV.items():monkeypatch.setenv(key,value)
    app_env(empty_db_url)
    owner=make_engine(empty_db_url)
    uid,held=uuid.uuid4(),uuid.uuid4()
    with Session(owner) as db:
        for key,email,role in ((uid,'c4-upgrade@example.org','admin'),(held,'c4-held@example.org','user')):
            db.add(User(id=key,email=email,role=role,password_verifier=hash_password(LEGACY_SECRET),
                        password_changed_at=NOW,created_at=NOW,updated_at=NOW))
        db.commit()
    w=SimpleNamespace(engine=owner,path=journal_path,settings=get_settings())
    runtime=runtime_engine(w)
    try:
        operator_credential(w,uid,password=SECRET)
        operator_initial_status(w,uid)
        with runtime.connect() as conn:
            proven=conn.execute(text('SELECT credential_operation_id,status_operation_id FROM users WHERE id=:u'),{'u':uid}).one()
        recovery.hold(owner,w.settings,utcnow())  # current binary, actual old schema
        upgrade(empty_db_url)
        runtime_engine(w).dispose()
        app_env(runtime.url)
        assert TestClient(app).get('/readyz').status_code==503
        result=recovery.recover(owner,SimpleNamespace(),get_settings(),utcnow())
        assert result.safety_ready
        assert TestClient(app).get('/readyz').status_code==200
        assert Browser().login('c4-upgrade@example.org',SECRET).status_code==200
        assert Browser().login('c4-held@example.org',LEGACY_SECRET).status_code==401
        with runtime.connect() as conn:
            assert conn.execute(text('SELECT credential_operation_id,status_operation_id FROM users WHERE id=:u'),{'u':uid}).one()==proven
            assert conn.execute(text('SELECT credential_state,status_safety FROM users WHERE id=:u'),{'u':held}).one()==('quarantined','held')
    finally:
        runtime.dispose();owner.dispose()
