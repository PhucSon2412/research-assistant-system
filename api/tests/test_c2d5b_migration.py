"""0040 populated/provisioned upgrade to 0041, reprovision and downgrade, no dev DB."""
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app import auth_safety, recovery
from app.config import get_settings
from app.db_roles import provision
from app.identity.bootstrap import bootstrap_admin
from tests.conftest import alembic_config, register_new_install, upgrade
from tests.test_auth_safety_foundation import NOW, SECRET, runtime_engine

ROUTINES = ('ras_auth_review_approval_mirror(text)', 'ras_auth_review_approval_confirm(text)')
OLD = ('ras_auth_review_closure_mirror(text)', 'ras_auth_review_closure_confirm(text)')


def test_0040_provisioned_populated_upgrade_and_reprovision(empty_db_url, journal_path, app_env, monkeypatch):
    upgrade(empty_db_url, '0040')
    register_new_install(empty_db_url, journal_path)
    app_env(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    w = SimpleNamespace(engine=eng, path=journal_path, settings=get_settings())
    rt = runtime_engine(w)  # roles provisioned at 0040, before the new routines exist
    try:
        monkeypatch.setattr(auth_safety, 'cutover', lambda db: 'ready')
        uid = bootstrap_admin(rt, email='upgrade@example.test', password=SECRET, policy=w.settings.password,
                              clock=lambda: NOW).user_id
        with eng.begin() as c:
            item = c.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:u,'t')"
                                 " RETURNING id"), {'u': uid})
            version = c.scalar(text('INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id'), {'i': item})
            file = c.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,byte_size,verified_mime,"
                                 "checksum_sha256) VALUES(:i,:v,'fixture',1,'text/markdown',repeat('a',64)) RETURNING id"),
                            {'i': item, 'v': version})
            c.execute(text("INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,"
                           "file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,"
                           "submitted_at) VALUES(:i,:v,:f,:u,'open',repeat('a',64),0,'test','s','r',:at)"),
                      {'i': item, 'v': version, 'f': file, 'u': uid, 'at': NOW})
        raw = journal_path.read_bytes()
        with eng.connect() as c:
            before = c.execute(text('SELECT * FROM review_requests')).all()
            old_acl = {fn: c.scalar(text('SELECT proacl::text FROM pg_proc WHERE oid=to_regprocedure(:f)'), {'f': fn})
                       for fn in OLD}
        upgrade(empty_db_url)
        for repeat in (False, True):
            if repeat:
                provision(eng, runtime='c1_runtime', recovery='c1_recovery', definer='c1_definer')
            with eng.connect() as c:
                assert c.execute(text('SELECT * FROM review_requests')).all() == before
                for fn in ROUTINES:
                    assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"), {'f': fn})
                    assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"), {'f': fn})
                    assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL"
                                             " aclexplode(p.proacl) a WHERE p.oid=to_regprocedure(:f) AND a.grantee=0)"),
                                        {'f': fn})
                    row = c.execute(text("SELECT r.rolname,r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p"
                                         " JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:f)"),
                                    {'f': fn}).one()
                    assert row[0] == 'c1_definer' and row[1] is False and row[2] is True
                    assert 'search_path=pg_catalog, public, pg_temp' in row[3]
                for fn in OLD:  # 0040 withdraw/reject routines keep their runtime-only EXECUTE
                    if not repeat:  # the upgrade itself leaves their ACL byte-identical
                        assert c.scalar(text('SELECT proacl::text FROM pg_proc WHERE oid=to_regprocedure(:f)'),
                                        {'f': fn}) == old_acl[fn]
                    assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"), {'f': fn})
                    assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"), {'f': fn})
                for table in ('access_rights', 'library_items', 'paper_versions', 'source_files', 'review_requests'):
                    assert c.scalar(text("SELECT has_table_privilege('c1_definer',:t,'SELECT')"), {'t': table})
                    assert not c.scalar(text("SELECT has_table_privilege('c1_definer',:t,'INSERT')"), {'t': table})
                for priv in ('INSERT', 'UPDATE', 'DELETE'):
                    assert not c.scalar(text("SELECT has_table_privilege('c1_runtime','revocation_journal_records',:p)"),
                                        {'p': priv})
                assert not c.scalar(text("SELECT has_database_privilege('c1_runtime',current_database(),'TEMP')"))
                assert not c.scalar(text("SELECT has_schema_privilege('c1_runtime','public','CREATE')"))
            assert raw == journal_path.read_bytes()
        # Runtime reaches the routine body (its own check), not a permission error.
        with rt.connect() as c, pytest.raises(DBAPIError, match='invalid review approval envelope'):
            c.execute(text('SELECT ras_auth_review_approval_mirror(:l)'), {'l': '{}'})
        with pytest.raises(DBAPIError, match='auth safety history prevents downgrade'):
            command.downgrade(alembic_config(empty_db_url), '0040')
        assert recovery.evaluate_integrity(w.settings, rt).open
    finally:
        rt.dispose()
        eng.dispose()


def test_0041_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), '0040')
    eng = create_engine(empty_db_url, hide_parameters=True)
    try:
        with eng.connect() as c:
            assert all(c.scalar(text('SELECT to_regprocedure(:f)'), {'f': fn}) is None for fn in ROUTINES)
            assert all(c.scalar(text('SELECT to_regprocedure(:f)'), {'f': fn}) is not None for fn in OLD)
        upgrade(empty_db_url)
        with eng.connect() as c:
            assert all(c.scalar(text('SELECT to_regprocedure(:f)'), {'f': fn}) is not None for fn in ROUTINES)
    finally:
        eng.dispose()
