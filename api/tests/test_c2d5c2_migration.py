"""0041 populated/provisioned -> 0042, fresh ACL, downgrade fences."""
from types import SimpleNamespace
from datetime import timedelta
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, recovery
from app.clock import utcnow
from app.config import get_settings
from app.db_roles import provision
from app.identity.bootstrap import bootstrap_admin
from app.models import DeletionTombstone, ReviewRequest, RevocationMetadataRetention
from tests.conftest import alembic_config, register_new_install, upgrade
from tests.test_auth_safety_foundation import NOW, SECRET, runtime_engine
from tests.test_migrations import _reviewed_copy_0024

ROUTINES=('ras_auth_common_revocation_mirror(text)','ras_auth_common_revocation_confirm(text)')


def test_0041_populated_upgrade_acl_reprovision_preserves_journal_retention(empty_db_url,journal_path,app_env,monkeypatch):
    upgrade(empty_db_url,'0041');register_new_install(empty_db_url,journal_path);app_env(empty_db_url)
    eng=create_engine(empty_db_url,hide_parameters=True)
    w=SimpleNamespace(engine=eng,path=journal_path,settings=get_settings())
    rt=runtime_engine(w)
    try:
        monkeypatch.setattr(auth_safety,'cutover',lambda db:'ready')
        uid=bootstrap_admin(rt,email='upgrade@example.test',password=SECRET,policy=w.settings.password,clock=lambda:NOW).user_id
        # Labelled historical system seed, NOT the runtime revoke under test.
        # The old schema has no new ORM marker columns: use explicit old SQL.
        with eng.begin() as c:
            c.execute(text("SET LOCAL session_replication_role='replica'"))
            _,_,item,version,file=_reviewed_copy_0024(c,uid,uid,h='a'*64,label='upgrade')
            key='originals/'+uuid.uuid4().hex
            c.execute(text('UPDATE source_files SET storage_key=:k WHERE id=:f'),dict(k=key,f=file))
        with Session(eng) as d:
            rr=d.query(ReviewRequest).one()
            recovery.record_review_closure(d,journal_path,rr);d.commit()
        at=utcnow()+timedelta(seconds=1)
        with Session(eng) as d:
            ar=d.scalar(text('SELECT id FROM access_rights WHERE item_id=:i'),{'i':item})
            d.execute(text("UPDATE library_items SET publication_state='revoked',rights_epoch=1 WHERE id=:i"),{'i':item})
            d.execute(text("UPDATE access_rights SET revoked_at=:at,revoked_by_user_id=:u,revocation_reason_code='other',rights_epoch_at_revoke=1 WHERE id=:a"),dict(at=at,u=uid,a=ar))
            tomb=DeletionTombstone(target_kind='common_copy',item_id=item,version_id=version,source_file_id=file,
                storage_key=key,access_rights_id=ar,reason_code='other',prior_publication_state='pending',rights_epoch=1,
                recorded_at=at,recorded_by_user_id=uid,purge_due_at=at+timedelta(days=1))
            d.add(tomb);d.flush()
            basis=RevocationMetadataRetention(tombstone_id=tomb.id,item_id=item,field='title',policy_source='synthetic policy',
                scope='one copy',reason='synthetic basis',confirmed_by_user_id=uid,recorded_at=at)
            d.add(basis);d.flush();recovery.record_revocation(d,journal_path,tomb,[basis]);d.commit()
        before=journal_path.read_bytes()
        with eng.connect() as c:
            rows={t:c.execute(text(f'SELECT * FROM {t} ORDER BY 1')).all() for t in ('deletion_tombstones','revocation_metadata_retentions','revocation_journal_records')}
        upgrade(empty_db_url,'0042')  # this revision's own contract; 0044 has its own upgrade test
        for repeat in (False,True):
            if repeat: provision(eng,runtime='c1_runtime',recovery='c1_recovery',definer='c1_definer')
            with eng.connect() as c:
                assert c.scalar(text('SELECT version_num FROM alembic_version'))=='0042'
                for table,expected in rows.items(): assert c.execute(text(f'SELECT * FROM {table} ORDER BY 1')).all()==expected
                assert c.execute(text('SELECT revocation_transaction_id,revocation_prior_state,revocation_prior_epoch FROM library_items WHERE id=:i'),{'i':item}).one()==(None,None,None)
                for fn in ROUTINES:
                    assert c.scalar(text("SELECT has_function_privilege('c1_runtime',:f,'EXECUTE')"),{'f':fn})
                    assert not c.scalar(text("SELECT has_function_privilege('c1_recovery',:f,'EXECUTE')"),{'f':fn})
                    assert not c.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE p.oid=to_regprocedure(:f) AND a.grantee=0)'),{'f':fn})
                    r=c.execute(text('SELECT r.rolcanlogin,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid=to_regprocedure(:f)'),{'f':fn}).one()
                    assert r[0] is False and r[1] is True and 'search_path=pg_catalog, public, pg_temp' in r[2]
                for priv in ('INSERT','UPDATE','DELETE'):
                    assert not c.scalar(text("SELECT has_table_privilege('c1_runtime','revocation_journal_records',:p)"),{'p':priv})
                assert not c.scalar(text("SELECT has_database_privilege('c1_runtime',current_database(),'TEMP')"))
            assert journal_path.read_bytes()==before and recovery.evaluate_integrity(w.settings,rt).open
        with rt.connect() as c,pytest.raises(DBAPIError): c.execute(text('SELECT ras_auth_common_revocation_mirror(NULL)'))
        with pytest.raises(DBAPIError,match='auth safety history prevents downgrade'):
            command.downgrade(alembic_config(empty_db_url),'0041')
    finally: rt.dispose();eng.dispose()


def test_0042_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url),'0041')
    upgrade(empty_db_url)
