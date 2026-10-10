"""0043 provisioned -> 0044: least grants for the fence, reprovision, downgrade fence."""
from types import SimpleNamespace
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.db_roles import provision
from tests.conftest import alembic_config, register_new_install, upgrade
from tests.test_auth_safety_foundation import runtime_engine

ROUTINE = 'ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)'


def test_0043_provisioned_upgrade_grants_and_downgrade_fence(empty_db_url, journal_path, app_env):
    upgrade(empty_db_url, '0043'); register_new_install(empty_db_url, journal_path); app_env(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    w = SimpleNamespace(engine=eng, path=journal_path, settings=get_settings())
    rt = runtime_engine(w)  # provisions 0043 roles before the fence exists
    try:
        upgrade(empty_db_url, '0044')
        for repeat in (False, True):
            if repeat:
                provision(eng, runtime='c1_runtime', recovery='c1_recovery', definer='c1_definer')
            with eng.connect() as c:
                assert c.scalar(text('SELECT version_num FROM alembic_version')) == '0044'
                for role in ('c1_runtime', 'c1_recovery'):
                    assert c.scalar(text("SELECT has_table_privilege(:r,'object_delete_fences','SELECT')"), {'r': role})
                    for priv in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES'):
                        assert not c.scalar(text("SELECT has_table_privilege(:r,'object_delete_fences',:p)"),
                                            {'r': role, 'p': priv}), (role, priv)
                assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"), {'f': ROUTINE})
                assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"), {'f': ROUTINE})
                assert not c.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a'
                                         ' WHERE p.oid=to_regprocedure(:f) AND a.grantee=0)'), {'f': ROUTINE})
                owner = c.execute(text('SELECT r.rolname,r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r'
                                       ' ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:f)'), {'f': ROUTINE}).one()
                assert owner[:3] == ('c1_definer', False, True) and 'search_path=pg_catalog, public, pg_temp' in owner[3]
                assert c.scalar(text("SELECT has_table_privilege('c1_definer','object_delete_fences','INSERT')"))
                assert not c.scalar(text("SELECT has_table_privilege('c1_definer','object_delete_fences','UPDATE')"))
        with rt.begin() as c, pytest.raises(DBAPIError, match='invalid fence binding'):
            c.execute(text('SELECT ras_auth_cleanup_fence(NULL,NULL,NULL,NULL,NULL)'))
        with eng.begin() as c:  # labelled setup: a used fence (no runtime write path exists for this)
            c.execute(text('INSERT INTO object_delete_fences(storage_key,tombstone_id,item_id,rights_epoch)'
                           ' VALUES(:k,:t,:i,1)'), {'k': 'originals/' + uuid.uuid4().hex, 't': uuid.uuid4(), 'i': uuid.uuid4()})
        with pytest.raises(DBAPIError, match='object deletion fences exist'):
            command.downgrade(alembic_config(empty_db_url), '0043')
        with eng.connect() as c:
            assert c.scalar(text('SELECT count(*) FROM object_delete_fences')) == 1
    finally:
        rt.dispose(); eng.dispose()


def test_0044_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), '0043')
    eng = create_engine(empty_db_url, hide_parameters=True)
    with eng.connect() as c:
        assert c.scalar(text("SELECT to_regclass('object_delete_fences')")) is None
        assert c.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname='trg_source_files_delete_fence'")) == 0
    eng.dispose()
    upgrade(empty_db_url)
