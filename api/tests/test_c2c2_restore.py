"""Real pg_dump/pg_restore on fresh DBs, current independent journal copies.

Only the isolated test container is addressed, checked by its task label and
explicit admin URL. No Compose/.env/object store or provider is needed.
"""
import json
import os
import subprocess
from types import SimpleNamespace

from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session

from app import auth_safety,journal,recovery
from app.models import Invitation,User
from tests.conftest import create_empty_database,drop_database
from tests.test_c2c2_invitations import world,issue,accept,EMAIL
from tests.test_c2c1_bootstrap import installation
from tests.test_auth_safety_foundation import NOW


def test_dump_restore_pre_accept_twice_current_journal(world,admin_engine,tmp_path,monkeypatch):
    w=world
    # The host runner sets these explicit identifiers after its ownership check.
    name=os.environ['RAS_C3_TEST_CONTAINER']
    details=json.loads(subprocess.run(['docker','inspect',name],check=True,capture_output=True,text=True).stdout)[0]
    assert details['Config']['Labels'].get('ras.task')==os.environ['RAS_C3_TEST_PROJECT']
    assert admin_engine.url.host=='127.0.0.1'
    assert details['NetworkSettings']['Ports']['5432/tcp']==[{'HostIp':'127.0.0.1','HostPort':str(admin_engine.url.port)}]
    created=issue(w)
    dump=subprocess.run(['docker','exec',name,'pg_dump','-U',admin_engine.url.username,'-Fc','-d',w.engine.url.database],check=True,capture_output=True,timeout=60).stdout
    assert dump.startswith(b'PGDMP')
    (tmp_path/'pre-accept.dump').write_bytes(dump)
    accepted=accept(w,created)
    source=w.path.read_bytes()
    for iteration in range(2):
        url=create_empty_database(admin_engine)
        rc=rt=None
        try:
            subprocess.run(['docker','exec','-i',name,'pg_restore','-U',admin_engine.url.username,'-d',url.database],input=dump,check=True,capture_output=True,timeout=60)
            rc=create_engine(url.set(username='c1_recovery',password=os.environ['RAS_C1_RECOVERY_PASSWORD']),hide_parameters=True)
            rt=create_engine(url.set(username='c1_runtime',password=os.environ['RAS_C1_RUNTIME_PASSWORD']),hide_parameters=True)
            copy=tmp_path/f'restore-{iteration}'/'journal.jsonl';copy.parent.mkdir();copy.write_bytes(source)
            settings=w.settings.model_copy(deep=True);settings.recovery.journal_path=copy
            recovery.hold(rc,settings,NOW)
            assert not recovery.evaluate_integrity(settings,rt).open
            for _ in range(2):
                report=recovery.recover(rc,SimpleNamespace(),settings,NOW)
                assert report.reconciliation_complete and not report.safety_ready
                with Session(rt) as db:
                    inv=db.get(Invitation,created._binding.invitation_id)
                    assert inv.closed_by_recovery_at and inv.accepted_at is inv.accepted_user_id is None
                    assert inv.recovery_end_kind=='accepted'
                    assert db.get(User,accepted.id) is None
                    assert db.scalar(text("SELECT count(*) FROM auth_change_receipts WHERE reason_code='invitation_accept'"))==0
                    assert not auth_safety.account_live(db,accepted.id)
                    assert db.scalar(text('SELECT count(*) FROM users WHERE email=:email'),{'email':EMAIL})==0
                    assert db.scalar(text('SELECT count(*) FROM invitations WHERE id=:id AND accepted_at IS NULL AND revoked_at IS NULL AND closed_by_recovery_at IS NULL'),{'id':inv.id})==0
            assert w.path.read_bytes()==source
            with monkeypatch.context() as patch:
                # The final production pending boundary remains closed.
                patch.setattr(auth_safety,'cutover',lambda db:'auth_safety_pending')
                assert recovery.evaluate(settings,rt).reason=='auth_safety_pending'
        finally:
            if rt:rt.dispose()
            if rc:rc.dispose()
            drop_database(admin_engine,url)
