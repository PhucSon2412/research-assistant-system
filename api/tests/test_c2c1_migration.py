"""0036 legacy singleton/history remains unproven; runtime ACL survives upgrade."""
import uuid
from types import SimpleNamespace
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from app.config import get_settings
from app import recovery
from app.identity.passwords import hash_password
from tests.conftest import upgrade, register_new_install, alembic_config, HEAD_REVISION
from tests.test_auth_safety_foundation import runtime_engine, append_changes, NOW, SECRET


def test_0036_upgrade_preserves_legacy_singleton_history_and_grants(empty_db_url, journal_path, app_env):
    upgrade(empty_db_url, "0036")
    register_new_install(empty_db_url, journal_path)
    app_env(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    uid = uuid.uuid4()
    w = SimpleNamespace(engine=eng, uid=uid, path=journal_path, settings=get_settings())
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO users(id,email,role,password_verifier,password_changed_at)"
                          " VALUES (:uid,'legacy@example.test','admin',:v,:at)"),
                     {"uid": uid, "v": hash_password(SECRET), "at": NOW})
        conn.execute(text("INSERT INTO bootstrap_state(admin_user_id) VALUES (:uid)"), {"uid": uid})
        conn.execute(text("INSERT INTO users(email,password_verifier) VALUES ('unproven@example.test',:v)"),
                     {"v": hash_password(SECRET)})
    rt = runtime_engine(w)
    try:
        append_changes(w, engine=rt)
        recovery.recover(eng, SimpleNamespace(), w.settings, NOW)
        before = journal_path.read_bytes()
        with eng.connect() as conn:
            rows = {t: conn.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all()
                    for t in ("users", "bootstrap_state", "auth_change_receipts", "auth_safety_heads", "revocation_journal_records")}
        upgrade(empty_db_url)
        with eng.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
            for table, values in rows.items():
                after = conn.execute(text(f"SELECT row_to_json(r) FROM {table} r")).scalars().all()
                if table == "bootstrap_state":
                    assert after[0].pop("creation_transaction_id") is None
                assert after == values
        assert journal_path.read_bytes() == before
        with rt.connect() as conn:
            signature = "ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)"
            assert conn.scalar(text("SELECT has_function_privilege(current_user,:fn,'EXECUTE')"), {"fn": signature})
            assert not conn.scalar(text("SELECT has_function_privilege('c1_recovery',:fn,'EXECUTE')"), {"fn": signature})
            assert not conn.scalar(text("SELECT has_table_privilege(current_user,'bootstrap_state','INSERT')"
                                       " OR has_table_privilege(current_user,'users','INSERT')"))
            assert not conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a"
                                       " WHERE p.oid=to_regprocedure(:fn) AND a.grantee=0 AND a.privilege_type='EXECUTE')"), {"fn": signature})
            assert conn.scalar(text("SELECT ras_auth_account_live(:uid)"), {"uid": uid})
        assert recovery.evaluate(w.settings, rt).reason == "auth_safety_pending"
        with pytest.raises(DBAPIError, match="auth safety history prevents downgrade"):
            command.downgrade(alembic_config(empty_db_url), "0036")
    finally:
        rt.dispose(); eng.dispose()


def test_0037_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), "0036")
    eng = create_engine(empty_db_url)
    try:
        with eng.connect() as conn:
            assert conn.scalar(text("SELECT to_regprocedure('ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)')")) is None
        upgrade(empty_db_url)
    finally:
        eng.dispose()


@pytest.mark.parametrize("mode", ["new_install", "existing_db"])
def test_0037_to_0038_preserves_accounts_binding_owner_and_acl(
    empty_db_url, journal_path, app_env, monkeypatch, mode,
):
    from sqlalchemy.orm import Session
    from app import auth_safety
    from app.identity.session_commit import AUTH_PENDING, commit_auth
    from tests.test_c2c1_bootstrap import prepare, initial

    upgrade(empty_db_url, "0037")
    app_env(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    w = SimpleNamespace(engine=eng, path=journal_path, settings=get_settings())
    rt = None
    signature = "ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)"
    tables = ("users", "bootstrap_state", "auth_change_receipts", "auth_safety_heads",
              "auth_safety_state", "recovery_registrations", "revocation_journal_records", "audit_events")
    original_cutover = auth_safety.cutover
    try:
        if mode == "new_install": recovery.init_new(eng, w.settings, NOW)
        else: recovery.register_existing(eng, w.settings, NOW)
        rt = runtime_engine(w)
        w.runtime = rt
        app_env(rt.url)
        w.settings = get_settings()
        monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
        if mode == "new_install":
            with Session(rt) as db:
                uid, params = prepare(w, db)
                db.info[AUTH_PENDING] = initial(uid, params)
                commit_auth(db)
        recovery.recover(eng, SimpleNamespace(), w.settings, NOW)
        before_bytes = journal_path.read_bytes()
        with eng.begin() as conn:
            # Emulate a previously provisioned 0037 definer, lacking the new read.
            conn.execute(text("REVOKE SELECT ON recovery_registrations FROM c1_definer"))
            acl_before = conn.execute(text("SELECT proowner,proacl::text,proconfig FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                                      {"fn": signature}).one()
            before = {t: conn.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all() for t in tables}
        upgrade(empty_db_url)
        with eng.connect() as conn:
            assert conn.execute(text("SELECT proowner,proacl::text,proconfig FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                                {"fn": signature}).one() == acl_before
            assert conn.scalar(text("SELECT has_table_privilege('c1_definer','recovery_registrations','SELECT')"))
            assert {t: conn.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all() for t in tables} == before
        assert journal_path.read_bytes() == before_bytes
        with Session(rt) as db:
            if mode == "new_install": assert auth_safety.account_live(db, uid)
            with pytest.raises(DBAPIError, match=("bootstrap already completed" if mode == "new_install"
                                                 else "bootstrap requires bound new installation")):
                prepare(w, db)
            db.rollback()
        for target in ("0037", "0036"):
            with pytest.raises(DBAPIError, match="auth safety history prevents downgrade"):
                command.downgrade(alembic_config(empty_db_url), target)
        monkeypatch.setattr(auth_safety, "cutover", original_cutover)
        assert recovery.evaluate(w.settings, rt).reason == "auth_safety_pending"
    finally:
        if rt is not None: rt.dispose()
        eng.dispose()


def test_0038_pristine_roundtrip_restores_predecessor(empty_db_url):
    upgrade(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    signature = "ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)"
    try:
        command.downgrade(alembic_config(empty_db_url), "0037")
        with eng.connect() as conn:
            source = conn.scalar(text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"), {"fn": signature})
            assert "bootstrap requires bound new installation" not in source
        upgrade(empty_db_url)
        with eng.connect() as conn:
            source = conn.scalar(text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"), {"fn": signature})
            assert "bootstrap requires bound new installation" in source
            assert not conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a"
                                       " WHERE p.oid=to_regprocedure(:fn) AND a.grantee=0 AND a.privilege_type='EXECUTE')"),
                                   {"fn": signature})
    finally:
        eng.dispose()
