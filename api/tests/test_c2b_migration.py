"""Populated 0035 upgrade with provisioned LOGIN roles and immutable history."""
import uuid
from types import SimpleNamespace
import pytest
from alembic import command
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app import recovery
from app.config import get_settings
from app.models import User
from app.identity.passwords import hash_password
from tests.conftest import upgrade, register_new_install, alembic_config, HEAD_REVISION
from tests.test_auth_safety_foundation import runtime_engine, append_changes, NOW, SECRET


def test_0035_populated_upgrade_preserves_history_roles_and_pending(empty_db_url,journal_path,app_env):
    upgrade(empty_db_url,"0035");register_new_install(empty_db_url,journal_path);app_env(empty_db_url)
    eng=create_engine(empty_db_url,hide_parameters=True);uid=uuid.uuid4()
    w=SimpleNamespace(engine=eng,uid=uid,path=journal_path,settings=get_settings())
    with Session(eng) as db:
        db.add(User(id=uid,email="c1@example.test",password_verifier=hash_password(SECRET),password_changed_at=NOW));db.commit()
    rt=runtime_engine(w)
    try:
        append_changes(w,engine=rt);recovery.hold(eng,w.settings,NOW)
        before=journal_path.read_bytes()
        with eng.connect() as conn:
            rows={t:conn.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all()
                  for t in ("users","auth_change_receipts","auth_safety_heads","revocation_journal_records")}
        upgrade(empty_db_url)
        with eng.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version"))==HEAD_REVISION
            for t,values in rows.items():
                after=conn.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all()
                if t=="revocation_journal_records":
                    for row in after:
                        for col in ("case_id","case_operator_id","case_user_id","case_action","case_operation_id","case_recorded_at"):
                            assert row.pop(col) is None
                assert after==values
        assert journal_path.read_bytes()==before
        recovery.recover(eng,SimpleNamespace(),w.settings,NOW)
        from tests.test_c3_operator_recovery import operator_initial_status
        operator_initial_status(w,uid)
        with rt.begin() as conn:
            assert conn.scalar(text("SELECT ras_auth_status_live(:uid)"),{"uid":uid})
            assert conn.scalar(text("SELECT ras_auth_reset_eligible(:uid,(SELECT credential_operation_id FROM users WHERE id=:uid),'normal')"),{"uid":uid})
            assert not conn.scalar(text("SELECT has_table_privilege(current_user,'password_reset_tokens','UPDATE')"))
            assert not conn.scalar(text("SELECT has_table_privilege(current_user,'users','UPDATE')"))
            for signature in ("ras_auth_token_lock(uuid,uuid[])","ras_auth_token_close(uuid,uuid,text,text,timestamptz)"):
                assert conn.scalar(text("SELECT has_function_privilege(current_user,:fn,'EXECUTE')"),{"fn":signature})
                assert conn.scalar(text("SELECT has_function_privilege('c1_recovery',:fn,'EXECUTE')"),{"fn":signature})
                assert not conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE p.oid=to_regprocedure(:fn) AND a.grantee=0 AND a.privilege_type='EXECUTE')"),{"fn":signature})
            result=conn.execute(text("SELECT r.rolcanlogin,p.proconfig FROM pg_proc p JOIN pg_roles r ON p.proowner=r.oid WHERE p.proname='ras_auth_token_close'")).one()
            assert not result[0] and "search_path=pg_catalog, public, pg_temp" in result[1]
        assert recovery.evaluate(w.settings,eng).reason=="auth_safety_pending"
        with pytest.raises(DBAPIError,match="auth safety history prevents downgrade"):
            command.downgrade(alembic_config(empty_db_url),"0035")
    finally:rt.dispose();eng.dispose()


def test_0036_pristine_downgrade_and_upgrade(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url),"0035")
    eng=create_engine(empty_db_url)
    try:
        with eng.connect() as conn:
            assert conn.scalar(text("SELECT to_regprocedure('ras_auth_reset_eligible(uuid,uuid,text)')")) is None
        upgrade(empty_db_url)
    finally:eng.dispose()
