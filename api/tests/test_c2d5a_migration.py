"""0039 populated/provisioned upgrade and pristine downgrade, no dev DB."""
from types import SimpleNamespace
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from app import auth_safety, recovery
from app.config import get_settings
from app.db_roles import provision
from app.identity.bootstrap import bootstrap_admin
from tests.conftest import upgrade, register_new_install, alembic_config
from tests.test_auth_safety_foundation import runtime_engine, NOW, SECRET


def test_0039_provisioned_populated_upgrade_and_reprovision(empty_db_url,journal_path,app_env,monkeypatch):
    upgrade(empty_db_url,'0039');register_new_install(empty_db_url,journal_path);app_env(empty_db_url)
    eng=create_engine(empty_db_url,hide_parameters=True)
    w=SimpleNamespace(engine=eng,path=journal_path,settings=get_settings());rt=runtime_engine(w)
    try:
        monkeypatch.setattr(auth_safety,'cutover',lambda db:'ready')
        uid=bootstrap_admin(rt,email='upgrade@example.test',password=SECRET,policy=w.settings.password,clock=lambda:NOW).user_id
        with eng.begin() as c:
            item=c.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:u,'test') RETURNING id"),{'u':uid})
            version=c.scalar(text('INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id'),{'i':item})
            file=c.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,byte_size,verified_mime,checksum_sha256) VALUES(:i,:v,'fixture',1,'text/markdown',repeat('a',64)) RETURNING id"),{'i':item,'v':version})
            rid=c.scalar(text("INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at) VALUES(:i,:v,:f,:u,'open',repeat('a',64),0,'test','s','r',:at) RETURNING id"),{'i':item,'v':version,'f':file,'u':uid,'at':NOW})
        from tests.test_review_requests import journal_sql_close
        with eng.begin() as c:
            c.execute(text("UPDATE review_requests SET status='withdrawn',closed_at=:at,closed_by_user_id=requester_user_id,close_reason_code='withdrawn_by_requester' WHERE id=:r"),{'at':NOW,'r':rid})
            journal_sql_close(c,rid)
        raw=journal_path.read_bytes()
        with eng.connect() as c:
            before=c.execute(text('SELECT id,status,closed_at,close_reason_code FROM review_requests')).all()
        upgrade(empty_db_url)
        for repeat in (False,True):
            if repeat:provision(eng,runtime='c1_runtime',recovery='c1_recovery',definer='c1_definer')
            with eng.connect() as c:
                assert c.execute(text('SELECT id,status,closed_at,close_reason_code FROM review_requests')).all()==before
                assert c.scalar(text('SELECT closure_transaction_id FROM review_requests')) is None
                for fn in ('ras_auth_review_closure_mirror(text)','ras_auth_review_closure_confirm(text)'):
                    assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:fn,'EXECUTE')"),{'fn':fn})
                    assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:fn,'EXECUTE')"),{'fn':fn})
                    assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE p.oid=to_regprocedure(:fn) AND a.grantee=0)"),{'fn':fn})
                    row=c.execute(text("SELECT r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:fn)"),{'fn':fn}).one()
                    assert row[0] is False and row[1] is True and 'search_path=pg_catalog, public, pg_temp' in row[2]
                assert not c.scalar(text("SELECT has_table_privilege('c1_runtime','revocation_journal_records','INSERT')"))
            assert raw==journal_path.read_bytes()
        with pytest.raises(DBAPIError,match='auth safety history prevents downgrade'):command.downgrade(alembic_config(empty_db_url),'0039')
        assert recovery.evaluate_integrity(w.settings,rt).open
    finally:rt.dispose();eng.dispose()


def test_0040_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url),'0039')
    eng=create_engine(empty_db_url,hide_parameters=True)
    try:
        with eng.connect() as c:assert c.scalar(text("SELECT to_regprocedure('ras_auth_review_closure_mirror(text)')")) is None
        upgrade(empty_db_url)
        with eng.connect() as c:assert c.scalar(text("SELECT to_regprocedure('ras_auth_review_closure_mirror(text)')")) is not None
    finally:eng.dispose()
