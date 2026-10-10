"""0044 provisioned with data -> 0045: narrow grants, both fences on INSERT, reprovision, downgrade refusal."""
from datetime import UTC, datetime, timedelta
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

FENCE = 'ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz)'
COMPLETE = 'ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz)'
INSERT_FILE = """INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,checksum_sha256)
    VALUES(:i,:v,'text/markdown',:k,1,repeat('a',64))"""


def item(c):
    i = c.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common','listed','m') RETURNING id"))
    return i, c.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id"), {'i': i})


def test_0044_provisioned_with_data_upgrade_grants_fences_and_downgrade(empty_db_url, journal_path, app_env):
    upgrade(empty_db_url, '0044'); register_new_install(empty_db_url, journal_path); app_env(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    w = SimpleNamespace(engine=eng, path=journal_path, settings=get_settings())
    rt = runtime_engine(w)  # provisions the 0044 roles before the orphan fence exists
    common_key, kept_key = 'originals/' + uuid.uuid4().hex, 'originals/' + uuid.uuid4().hex
    try:
        with eng.begin() as c:  # labelled 0044 data: a referenced file and a used revoked-common fence
            i, v = item(c)
            c.execute(text(INSERT_FILE), dict(i=i, v=v, k=kept_key))
            c.execute(text('INSERT INTO object_delete_fences(storage_key,tombstone_id,item_id,rights_epoch)'
                           ' VALUES(:k,gen_random_uuid(),gen_random_uuid(),1)'), {'k': common_key})
        upgrade(empty_db_url, '0045')
        for repeat in (False, True):
            if repeat:
                provision(eng, runtime='c1_runtime', recovery='c1_recovery', definer='c1_definer')
            with eng.connect() as c:
                assert c.scalar(text('SELECT version_num FROM alembic_version')) == '0045'
                for table in ('orphan_object_fences', 'orphan_object_completions'):
                    for role in ('c1_runtime', 'c1_recovery'):
                        assert c.scalar(text("SELECT has_table_privilege(:r,:t,'SELECT')"), {'r': role, 't': table})
                        for priv in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES'):
                            assert not c.scalar(text('SELECT has_table_privilege(:r,:t,:p)'),
                                                {'r': role, 't': table, 'p': priv}), (role, table, priv)
                    assert c.scalar(text("SELECT has_table_privilege('c1_definer',:t,'INSERT')"), {'t': table})
                    assert not c.scalar(text("SELECT has_table_privilege('c1_definer',:t,'UPDATE')"), {'t': table})
                for routine in (FENCE, COMPLETE):
                    assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"), {'f': routine})
                    assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"), {'f': routine})
                    owner = c.execute(text('SELECT r.rolname,r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r'
                                           ' ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:f)'), {'f': routine}).one()
                    assert owner[:3] == ('c1_definer', False, True)
                    assert 'search_path=pg_catalog, public, pg_temp' in owner[3]
                for role in ('c1_runtime', 'c1_recovery'):
                    assert c.scalar(text("SELECT has_table_privilege(:r,'orphan_reconcile_cursors','INSERT')"), {'r': role})
        at = datetime(2026, 10, 9, tzinfo=UTC)
        with rt.begin() as c:
            # Existing data keeps its meaning: the 0044 fence still refuses, the referenced key is never orphan-fenced.
            i, v = c.execute(text('SELECT item_id,version_id FROM source_files WHERE storage_key=:k'), {'k': kept_key}).one()
            assert c.scalar(text('SELECT ras_auth_orphan_fence(:r,:k,1,:m,:c,:t)'), dict(
                r=uuid.uuid4(), k=kept_key, m=at - timedelta(days=2), c=at - timedelta(days=1), t=at)) == 'referenced'
        with pytest.raises(DBAPIError, match='fenced for deletion'), rt.begin() as c:
            c.execute(text(INSERT_FILE), dict(i=i, v=v, k=common_key))
        orphan_key = 'originals/' + uuid.uuid4().hex
        with rt.begin() as c:
            assert c.scalar(text('SELECT ras_auth_orphan_fence(:r,:k,1,:m,:c,:t)'), dict(
                r=uuid.uuid4(), k=orphan_key, m=at - timedelta(days=2), c=at - timedelta(days=1), t=at)) == 'fenced'
        with pytest.raises(DBAPIError, match='fenced for deletion'), rt.begin() as c:
            c.execute(text(INSERT_FILE), dict(i=i, v=v, k=orphan_key))
        with pytest.raises(DBAPIError, match='orphan object fences exist'):
            command.downgrade(alembic_config(empty_db_url), '0044')
        with eng.connect() as c:
            assert c.scalar(text('SELECT count(*) FROM orphan_object_fences')) == 1
            assert c.scalar(text('SELECT count(*) FROM object_delete_fences')) == 1
    finally:
        rt.dispose(); eng.dispose()


def test_0045_pristine_roundtrip_restores_the_0044_trigger(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), '0044')
    eng = create_engine(empty_db_url, hide_parameters=True)
    with eng.connect() as c:
        for name in ('orphan_object_fences', 'orphan_object_completions', 'orphan_reconcile_cursors'):
            assert c.scalar(text('SELECT to_regclass(:t)'), {'t': name}) is None
        assert c.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname='trg_source_files_delete_fence'")) == 1
        assert 'orphan_object_fences' not in c.scalar(text(
            "SELECT prosrc FROM pg_proc WHERE proname='ras_source_files_delete_fence'"))
    eng.dispose()
    upgrade(empty_db_url)
