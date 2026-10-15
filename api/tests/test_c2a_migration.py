"""Upgrade populated/provisioned 0034 without weakening runtime ACL or pending."""
import uuid
from datetime import timedelta
from types import SimpleNamespace
import pytest
from alembic import command
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app import recovery, auth_safety
from app.models import User, UserSession, AuthChangeReceipt
from app.identity.passwords import hash_password
from app.config import get_settings
from tests.conftest import upgrade, register_new_install, alembic_config, HEAD_REVISION
from tests.test_auth_safety_foundation import runtime_engine, append_changes, NOW, SECRET, user


def test_0034_populated_provisioned_upgrade_preserves_history_and_permissions(empty_db_url,journal_path,app_env):
    upgrade(empty_db_url,'0034');register_new_install(empty_db_url,journal_path);app_env(empty_db_url)
    eng=create_engine(empty_db_url,hide_parameters=True)
    uid=uuid.uuid4();sid=uuid.uuid4()
    w=SimpleNamespace(engine=eng,uid=uid,path=journal_path,settings=get_settings())
    with Session(eng) as db:
        db.add(User(id=uid,email='migration@example.test',password_verifier=hash_password(SECRET),password_changed_at=NOW))
        db.commit()
    rt=runtime_engine(w)
    try:
        append_changes(w,engine=rt);before=user(w)
        with rt.begin() as conn:
            conn.execute(text('SELECT ras_auth_session_create(:sid,:uid,:gen,:token,:csrf,:at,:end)'),
                         {'sid':sid,'uid':uid,'gen':before.credential_generation,'token':'a'*64,'csrf':'b'*64,'at':NOW,'end':NOW+timedelta(hours=8)})
        recovery.hold(eng,w.settings,NOW)  # current recovery must follow migrate, not read an old schema
        with eng.connect() as conn:
            heads=conn.execute(text('SELECT row_to_json(h) FROM auth_safety_heads h')).scalars().all()
            progress=conn.execute(text('SELECT row_to_json(s) FROM auth_safety_state s')).scalars().all()
            receipts=conn.execute(text('SELECT row_to_json(r) FROM auth_change_receipts r ORDER BY operation_id')).scalars().all()
        journal_bytes=journal_path.read_bytes()
        upgrade(empty_db_url)
        with eng.connect() as conn:
            assert conn.scalar(text('SELECT version_num FROM alembic_version'))==HEAD_REVISION
            assert conn.execute(text('SELECT row_to_json(h) FROM auth_safety_heads h')).scalars().all()==heads
            assert conn.execute(text('SELECT row_to_json(s) FROM auth_safety_state s')).scalars().all()==progress
            assert conn.execute(text('SELECT row_to_json(r) FROM auth_change_receipts r ORDER BY operation_id')).scalars().all()==receipts
        assert journal_path.read_bytes()==journal_bytes
        recovery.recover(eng,SimpleNamespace(),w.settings,NOW)
        from tests.test_c3_operator_recovery import operator_initial_status
        operator_initial_status(w,uid)  # old admin_enable alone is not initial authority
        with rt.begin() as conn:
            assert conn.scalar(text('SELECT ras_auth_account_live(:uid)'),{'uid':uid})
            assert not conn.scalar(text('SELECT ras_auth_session_live(:sid,:uid,:at,:idle)'),{'sid':sid,'uid':uid,'at':NOW,'idle':timedelta(minutes=30)})
            # The C3 decision intentionally closes pre-verification sessions.
            # Issue a fresh session with the preserved credential generation.
            sid=uuid.uuid4()
            conn.execute(text('SELECT ras_auth_session_create(:sid,:uid,:gen,:token,:csrf,:at,:end)'),
                         {'sid':sid,'uid':uid,'gen':before.credential_generation,'token':'c'*64,'csrf':'d'*64,'at':NOW,'end':NOW+timedelta(hours=8)})
            assert conn.scalar(text('SELECT ras_auth_session_live(:sid,:uid,:at,:idle)'),{'sid':sid,'uid':uid,'at':NOW,'idle':timedelta(minutes=30)})
            assert conn.scalar(text('SELECT ras_auth_session_activity(:sid,:uid,:at,:idle,NULL)'),{'sid':sid,'uid':uid,'at':NOW,'idle':timedelta(minutes=30)})
            assert not conn.scalar(text("SELECT has_table_privilege(current_user,'sessions','UPDATE')"))
            for row in conn.execute(text("SELECT p.proowner=r.oid, p.proconfig, r.rolcanlogin FROM pg_proc p JOIN pg_roles r ON r.rolname='c1_definer' WHERE p.proname IN ('ras_auth_session_activity','ras_auth_session_close','ras_auth_closure_mirror')")):
                assert row[0] and 'search_path=pg_catalog, public, pg_temp' in row[1] and not row[2]
        assert recovery.evaluate(w.settings,eng).reason=='auth_safety_pending'
        with rt.begin() as conn,pytest.raises(DBAPIError):conn.execute(text('UPDATE sessions SET last_activity_at=now()'))
        with pytest.raises(DBAPIError,match='auth safety history prevents downgrade'):
            command.downgrade(alembic_config(empty_db_url),'0034')
    finally:rt.dispose();eng.dispose()


def test_0035_pristine_downgrade_and_upgrade(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url),'0034')
    upgrade(empty_db_url)
