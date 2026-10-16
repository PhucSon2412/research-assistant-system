"""Populated/provisioned 0038 -> 0039, least grants, pending and downgrade fence."""
import json
import uuid
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from alembic import command
from app import auth_safety,journal,recovery
from app.config import get_settings
from app.identity.passwords import hash_password
from tests.conftest import upgrade,register_new_install,alembic_config
from tests.test_auth_safety_foundation import runtime_engine,NOW,SECRET


def test_0038_populated_provisioned_upgrade(empty_db_url,journal_path,app_env,monkeypatch):
    upgrade(empty_db_url,'0038');register_new_install(empty_db_url,journal_path);app_env(empty_db_url)
    eng=create_engine(empty_db_url,hide_parameters=True)
    w=SimpleNamespace(engine=eng,path=journal_path,settings=get_settings())
    rt=runtime_engine(w)
    try:
        from app.identity.bootstrap import bootstrap_admin
        original_cutover=auth_safety.cutover
        monkeypatch.setattr(auth_safety,'cutover',lambda db:'ready')
        uid=bootstrap_admin(rt,email='admin@example.test',password=SECRET,policy=w.settings.password,clock=lambda:NOW).user_id
        with eng.begin() as db:
            db.execute(text("INSERT INTO invitations(email,token_hash,invited_by_user_id,created_at,expires_at) VALUES"
                            " ('legacy@example.test',repeat('e',64),:uid,:at,:at+interval '72 hours')"),{'uid':uid,'at':NOW})
        tables=['users','bootstrap_state','auth_change_receipts','auth_safety_heads','revocation_journal_records','audit_events']
        with eng.connect() as db:
            before={t:db.execute(text(f'SELECT row_to_json(r)::text FROM {t} r')).scalars().all() for t in tables}
            bootstrap=db.execute(text("SELECT oid,proowner,proacl::text FROM pg_proc WHERE proname='ras_auth_bootstrap_prepare'")).one()
        raw=journal_path.read_bytes()
        upgrade(empty_db_url)
        with eng.connect() as db:
            # Columns added by later revisions (0049 case_*) are NULL on every existing row; compare the rest.
            later=('case_id','case_operator_id','case_user_id','case_action','case_operation_id','case_recorded_at')
            for t in tables:
                after=[json.loads(v) for v in db.execute(text(f'SELECT row_to_json(r)::text FROM {t} r')).scalars().all()]
                assert all(row.pop(k,None) is None for row in after for k in later)
                assert after==[json.loads(v) for v in before[t]]
            assert db.execute(text("SELECT oid,proowner,proacl::text FROM pg_proc WHERE proname='ras_auth_bootstrap_prepare'")).one()==bootstrap
            assert db.execute(text('SELECT creation_transaction_id,acceptance_transaction_id FROM invitations')).one()==(None,None)
            for role in ('c1_runtime','c1_recovery'):
                for priv in ('INSERT','UPDATE','DELETE','TRUNCATE'):
                    assert not db.scalar(text("SELECT has_table_privilege(:r,'invitations',:p)"),{'r':role,'p':priv})
            for fn in ('ras_auth_invitation_create','ras_auth_invitation_prepare','ras_auth_invitation_compensate','ras_auth_invitation_revoke'):
                sig=db.scalar(text("SELECT oid::regprocedure::text FROM pg_proc WHERE proname=:fn"),{'fn':fn})
                assert db.scalar(text("SELECT has_function_privilege('c1_runtime',:fn,'EXECUTE')"),{'fn':sig})
                assert not db.scalar(text("SELECT has_function_privilege('c1_recovery',:fn,'EXECUTE')"),{'fn':sig})
                login,config,public=db.execute(text("SELECT r.rolcanlogin,p.proconfig,EXISTS(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE')"
                    " FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.proname=:fn"),{'fn':fn}).one()
                assert not login and not public and 'search_path=pg_catalog, public, pg_temp' in config
        assert journal_path.read_bytes()==raw
        with pytest.raises(DBAPIError,match='auth safety history prevents downgrade'):
            command.downgrade(alembic_config(empty_db_url),'0038')
        monkeypatch.setattr(auth_safety,'cutover',original_cutover)
        app_env(empty_db_url)
        runtime_engine(w).dispose()  # New contract routines/table require reprovision.
        assert recovery.evaluate(get_settings(),rt).reason=='auth_safety_pending'
    finally:rt.dispose();eng.dispose()


def test_0039_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    eng=create_engine(empty_db_url,hide_parameters=True)
    try:
        command.downgrade(alembic_config(empty_db_url),'0038')
        with eng.connect() as db:assert db.scalar(text("SELECT to_regprocedure('ras_auth_invitation_lock(uuid[])')")) is None
        upgrade(empty_db_url)
        with eng.connect() as db:assert db.scalar(text("SELECT to_regprocedure('ras_auth_invitation_lock(uuid[])')")) is not None
    finally:eng.dispose()
