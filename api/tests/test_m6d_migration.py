"""Data-bearing 0051 upgrade, actual old-schema hold and reprovision."""
import uuid
from types import SimpleNamespace
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app import recovery
from app.config import get_settings
from app.db import make_engine
from app.models import User
from app.identity.passwords import hash_password
from app.main import app
from tests.conftest import upgrade,register_new_install
from tests.helpers import DEV_ENV,Browser,WEB_ORIGIN
from tests.test_auth_safety_foundation import runtime_engine,NOW,SECRET,LEGACY_SECRET
from tests.test_c3_operator_recovery import operator_credential,operator_initial_status
from tests.test_m6d_private_delete import Storage


def test_0051_upgrade_preserves_private_data_then_real_runtime_delete(empty_db_url,journal_path,app_env,monkeypatch):
    upgrade(empty_db_url,'0051');register_new_install(empty_db_url,journal_path)
    for key,value in DEV_ENV.items():monkeypatch.setenv(key,value)
    app_env(empty_db_url)
    owner=make_engine(empty_db_url);uid=uuid.uuid4();item=uuid.uuid4();version=uuid.uuid4();file=uuid.uuid4()
    key='originals/'+uuid.uuid4().hex
    with Session(owner) as db:
        db.add(User(id=uid,email='m6d-upgrade@example.test',role='admin',password_verifier=hash_password(LEGACY_SECRET),
                    password_changed_at=NOW,created_at=NOW,updated_at=NOW));db.commit()
    w=SimpleNamespace(engine=owner,path=journal_path,settings=get_settings())
    runtime=runtime_engine(w)
    try:
        operator_credential(w,uid,password=SECRET);operator_initial_status(w,uid)
        with owner.begin() as c:
            c.execute(text("INSERT INTO library_items(id,visibility,owner_user_id,title) VALUES(:i,'private',:u,'M6D-0051-TITLE')"),{'i':item,'u':uid})
            c.execute(text("INSERT INTO paper_versions(id,item_id,source_url,version_label) VALUES(:v,:i,'https://private.example.test','v1')"),{'v':version,'i':item})
            c.execute(text("INSERT INTO source_files(id,item_id,version_id,storage_key,verified_mime,byte_size,checksum_sha256,original_filename)"
                " VALUES(:f,:i,:v,:k,'text/markdown',5,:h,'private.md')"),{'f':file,'i':item,'v':version,'k':key,'h':'a'*64})
        recovery.hold(owner,w.settings,NOW)
        upgrade(empty_db_url);runtime_engine(w).dispose();app_env(runtime.url)
        assert TestClient(app).get('/readyz').status_code==503
        assert recovery.recover(owner,Storage(SimpleNamespace(key=key)),get_settings(),NOW).safety_ready
        b=Browser(WEB_ORIGIN);assert b.login('m6d-upgrade@example.test',SECRET).status_code==200
        assert b.get(f'/library/items/{item}').json()['item']['title']=='M6D-0051-TITLE'
        response=b.post(f'/library/items/{item}/delete',{'confirm_delete':True})
        assert response.status_code==200,response.text
        assert b.get(f'/library/items/{item}').status_code==404
        assert TestClient(app).get('/readyz').status_code==200
    finally:
        runtime.dispose();owner.dispose()
