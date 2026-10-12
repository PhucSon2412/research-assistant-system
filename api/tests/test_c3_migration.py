"""0048 provisioned with history -> 0049: rows/journal/grants kept, operator boundary private, downgrade fenced."""
import os
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, recovery
from app.auth_safety import Change
from app.config import get_settings
from app.db_roles import OPERATOR_ROUTINES, provision
from app.identity.passwords import hash_password
from app.models import User
from tests.conftest import HEAD_REVISION, alembic_config, register_new_install, upgrade
from tests.test_auth_safety_foundation import NOW, SECRET, runtime_engine

ROUTINES = ("ras_auth_operator_whoami()", "ras_auth_operator_gate()", "ras_auth_operator_inspect(uuid)",
            "ras_auth_operator_case(uuid)", "ras_auth_operator_snapshot(uuid,timestamptz,boolean)",
            "ras_auth_operator_stage(uuid,timestamptz,text[],text,integer)", "ras_auth_operator_confirm(uuid,text[])",
            "ras_auth_operator_outcome(uuid)", "ras_auth_operator_assert()")


def test_0048_provisioned_history_upgrades_and_refuses_downgrade(empty_db_url, journal_path, app_env):
    upgrade(empty_db_url, "0048")
    register_new_install(empty_db_url, journal_path)
    app_env(empty_db_url)
    eng = create_engine(empty_db_url, hide_parameters=True)
    w = SimpleNamespace(engine=eng, path=journal_path, settings=get_settings())
    rt = runtime_engine(w)
    try:
        uid = uuid.uuid4()
        with Session(eng) as db:
            db.add(User(id=uid, email="c3-migration@example.test", role="admin", password_verifier=hash_password(SECRET),
                        password_changed_at=NOW, created_at=NOW, updated_at=NOW))
            db.commit()
        # Pre-C3 history: the generic path could still mint verified_* receipts; upgrade must keep them readable.
        with Session(rt) as db:
            db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
            recovery.record_auth_endings(db, journal_path, [], [], auth_changes=[
                Change(uid, "credential", "verified_recovery", NOW, verifier=hash_password(SECRET), policy_version=1),
                Change(uid, "status", "verified_initial_status", NOW, decision="active")])
            db.commit()
        with eng.connect() as c:
            receipts_before = c.execute(text("SELECT * FROM auth_change_receipts ORDER BY operation_id")).all()
            users_before = c.execute(text("SELECT id,credential_operation_id,status_operation_id,password_verifier FROM users")).all()
        journal_before = journal_path.read_bytes()
        upgrade(empty_db_url)
        for repeat in (False, True):
            if repeat:
                provision(eng, runtime="c1_runtime", recovery="c1_recovery", definer="c1_definer")
            with eng.connect() as c:
                assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
                assert c.execute(text("SELECT * FROM auth_change_receipts ORDER BY operation_id")).all() == receipts_before
                assert c.execute(text("SELECT id,credential_operation_id,status_operation_id,password_verifier FROM users")).all() == users_before
                for routine in ROUTINES:
                    owner, secdef, config = c.execute(text(
                        "SELECT pg_get_userbyid(proowner),prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:f)"),
                        {"f": routine}).one()
                    assert (owner, secdef) == ("c1_definer", True) and "search_path=pg_catalog, public, pg_temp" in config
                    for role in ("c1_runtime", "c1_recovery", "public"):
                        grantee = "0" if role == "public" else f"(SELECT oid FROM pg_roles WHERE rolname='{role}')"
                        assert not c.scalar(text(f"SELECT EXISTS(SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a"
                                                 f" WHERE p.oid=to_regprocedure(:f) AND a.grantee={grantee})"), {"f": routine})
                for table in ("auth_recovery_operators", "auth_recovery_cases"):
                    for role in ("c1_runtime", "c1_recovery"):
                        for priv in ("INSERT", "UPDATE", "DELETE"):
                            assert not c.scalar(text("SELECT has_table_privilege(:r,:t,:p)"), {"r": role, "t": table, "p": priv})
                    assert c.scalar(text("SELECT has_table_privilege('c1_definer',:t,'SELECT')"), {"t": table})
                    assert not c.scalar(text("SELECT has_table_privilege('c1_definer',:t,'INSERT')"), {"t": table})
            assert journal_path.read_bytes() == journal_before
        assert set(OPERATOR_ROUTINES) <= {r.split("(")[0] for r in ROUTINES}
        with Session(eng) as db:
            auth_safety.verify_snapshot(db)  # historical verified_* receipts still bind
        # After 0049 the same generic path is refused before any journal byte.
        with Session(rt) as db, pytest.raises(DBAPIError, match="operator recovery receipt needs its case use"):
            db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
            recovery.record_auth_endings(db, journal_path, [], [], auth_changes=[
                Change(uid, "credential", "verified_recovery", NOW, verifier=hash_password(SECRET), policy_version=1)])
        assert journal_path.read_bytes() == journal_before
        with pytest.raises(DBAPIError, match="auth safety history prevents downgrade"):
            command.downgrade(alembic_config(empty_db_url), "0048")
        with eng.connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
    finally:
        rt.dispose()
        eng.dispose()


def test_0049_pristine_roundtrip_restores_0048_allowlists(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), "0048")
    eng = create_engine(empty_db_url, hide_parameters=True)
    try:
        with eng.connect() as c:
            for name in ("auth_recovery_operators", "auth_recovery_cases"):
                assert c.scalar(text("SELECT to_regclass(:t)"), {"t": name}) is None
            assert c.scalar(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'ras_auth_operator_%'")) == 0
            assert c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='revocation_journal_records'"
                                 " AND column_name LIKE 'case_%'")) == 0
            for name in ("ck_sessions_revoked_reason_valid", "ck_password_reset_tokens_closure_reason_valid",
                         "ck_revocation_journal_records_kind_valid", "ck_revocation_journal_records_session_matches_body",
                         "ck_revocation_journal_records_reset_matches_body"):
                definition = c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=:n"), {"n": name})
                assert "operator_recovery" not in definition
        upgrade(empty_db_url)
        with eng.connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
            assert "operator_recovery" in c.scalar(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_sessions_revoked_reason_valid'"))
    finally:
        eng.dispose()
