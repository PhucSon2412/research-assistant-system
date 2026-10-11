"""C3 operator recovery against real PostgreSQL/journal/LOGIN roles; no authority/case/receipt patch.

Operator writes run in a real maintenance hold (``recover --hold-for-operator``)
and never call the pending cutover. Only tests that check runtime login/session
AFTER an outcome replace the pending cutover result in a scoped fixture (C1-C3
contract); integrity, hold, journal, case, receipt and session checks stay real.
"""
import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import auth_safety, journal, operator_recovery as ops, recovery
from app.auth_safety import Change
from app.config import get_settings
from app.db_roles import provision, provision_operator, register_case, revoke_operator
from app.identity.errors import InvalidCredentials
from app.identity.login import login
from app.identity.passwords import PasswordPolicyError, hash_password, verify_password
from app.identity.predicates import session_live
from app.models import AuthChangeReceipt, PasswordResetToken, User, UserSession
from app.operator_cases import SCHEMA, CaseError, load_case_file, parse_case
from tests.test_auth_safety_foundation import NOW, SECRET
from tests.test_c2c1_bootstrap import installation  # noqa: F401 - fixture

NEW = "C3-operator-new-password-marker-789!"
APPROVER = uuid.UUID("5f0e7c1d-8a4b-4c2e-9d3f-1a2b3c4d5e6f")
ROLES = (("c3_operator", "RAS_C3_OPERATOR_PASSWORD"), ("c3_operator2", "RAS_C3_OPERATOR2_PASSWORD"))


def ensure_login(w, name, env):
    import psycopg
    from psycopg import sql
    with psycopg.connect(w.engine.url.render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://"),
                         autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
                         .format(sql.Identifier(name), sql.Literal(os.environ[env])))
    return w.engine.url.set(username=name, password=os.environ[env])


@pytest.fixture
def world(installation, tmp_path):
    w = installation
    w.journal_id = journal.read(w.path).journal_id
    w.operator_id, w.operator2_id = uuid.uuid4(), uuid.uuid4()
    urls = [ensure_login(w, name, env) for name, env in ROLES]
    provision_operator(w.engine, login="c3_operator", operator_id=w.operator_id)
    provision_operator(w.engine, login="c3_operator2", operator_id=w.operator2_id)
    w.operator_url, w.operator2_url = urls
    w.operator = create_engine(urls[0], hide_parameters=True)
    w.operator2 = create_engine(urls[1], hide_parameters=True)
    w.recovery = create_engine(w.engine.url.set(username="c1_recovery", password=os.environ["RAS_C1_RECOVERY_PASSWORD"]),
                               hide_parameters=True)
    w.cases = tmp_path / "cases"
    w.cases.mkdir()
    try:
        yield w
    finally:
        for e in (w.operator, w.operator2, w.recovery):
            e.dispose()


def account(w, role="user", *, live=True, email=None, status="active"):
    uid = uuid.uuid4()
    with Session(w.engine) as db:
        db.add(User(id=uid, email=email or f"{uid.hex}@example.test", role=role, status=status,
                    password_verifier=hash_password(SECRET), password_changed_at=NOW, created_at=NOW, updated_at=NOW))
        db.commit()
    if live:
        journal_changes(w, uid, [Change(uid, "credential", "password_reset", NOW, verifier=hash_password(SECRET), policy_version=1),
                                 Change(uid, "status", "invitation_accept", NOW, decision="active")])
    return uid


def journal_changes(w, uid, values, *, rollback=False, tokens=(), sessions=()):
    with Session(w.runtime) as db:
        db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
        batch = recovery.record_auth_endings(db, w.path, list(tokens), list(sessions), auth_changes=values)
        db.rollback() if rollback else db.commit()
        return batch


def quarantine(w, uid):
    """A reset whose DB transaction rolled back after its journal line: the snapshot credential is quarantined."""
    journal_changes(w, uid, [Change(uid, "credential", "password_reset", NOW, verifier=hash_password(SECRET),
                                    policy_version=1)], rollback=True)
    recover(w)  # replay the journal-ahead line before the next writer


def hold_status(w, uid):
    """An enable intent journaled without its receipt: the account is active/held."""
    journal_changes(w, uid, [Change(uid, "status", "admin_enable", NOW, decision="active")], rollback=True)
    recover(w)


def recover(w, **kw):
    return recovery.recover(w.engine, SimpleNamespace(), w.settings, NOW, **kw)


def hold(w):
    recovery.hold(w.engine, w.settings, NOW)
    report = recover(w, hold_for_operator=True)
    assert report.held_for_operator and not report.reconciliation_complete and report.gate == "recovery_in_progress"
    return report


def heads(w, uid):
    credential, status = ops.journal_heads(journal.read(w.path), uid)
    return (str(credential.body["operation_id"]) if credential else None,
            str(status.body["operation_id"]) if status else None)


def scalar(w, sql, **params):
    with w.engine.connect() as conn:
        return conn.scalar(text(sql), params)


def make_case(w, uid, action, *, operator=None, register=True, role=None, approved=None, expires=None, **override):
    credential, status = heads(w, uid)
    body = {"schema": SCHEMA, "case_id": str(uuid.uuid4()), "action": action,
            "installation_journal_id": str(w.journal_id), "user_id": str(uid),
            "expected_role": role or scalar(w, "SELECT role FROM users WHERE id=:u", u=uid) or "user",
            "expected_credential_operation_id": credential, "expected_status_operation_id": status,
            "operator_id": str(operator or w.operator_id), "approver_id": str(APPROVER),
            "verification_method": "in_person_check",
            "approved_at": (approved or NOW - timedelta(minutes=5)).isoformat(),
            "expires_at": (expires or NOW + timedelta(hours=1)).isoformat()} | override
    path = w.cases / f"{body['case_id']}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    case = load_case_file(path, max_bytes=4096)
    if register:
        register_case(w.engine, case, now=NOW - timedelta(minutes=10), max_lifetime=w.settings.auth_recovery.case_max_lifetime)
    return case


def operate(w, case, *, engine=None, password=NEW, clock=lambda: NOW, **kw):
    return ops.run(engine or w.operator, w.settings, case, user_id=case.user_id, installation=case.journal_id,
                   action=case.action, clock=clock, read_password=lambda: password, **kw)


def user(w, uid):
    with Session(w.engine) as db:
        u = db.get(User, uid)
        db.expunge(u)
        return u


def counts(w):
    with w.engine.connect() as conn:
        return {t: conn.scalar(text(f"SELECT count(*) FROM {t}")) for t in (
            "users", "auth_change_receipts", "revocation_journal_records", "audit_events", "sessions",
            "password_reset_tokens")}


def sign_in(w, uid, password, monkeypatch):
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")  # scoped pending boundary only
    with Session(w.runtime, expire_on_commit=False) as db:
        return login(db, email=scalar(w, "SELECT email FROM users WHERE id=:u", u=uid), password=password, now=NOW,
                     settings=w.settings, clock=lambda: NOW)[1]


def live_session(w, uid, monkeypatch):
    """A real session issued while the account was live (before the hold)."""
    issued = sign_in(w, uid, SECRET, monkeypatch)
    monkeypatch.undo()
    return issued.session_id


def operator_initial_status(w, uid):
    """Real C3 path for other suites that need a verified_initial_status receipt (no shortcut exists)."""
    import shutil
    import tempfile
    from pathlib import Path
    url = ensure_login(w, "c3_operator", "RAS_C3_OPERATOR_PASSWORD")
    with w.engine.connect() as conn:
        existing = conn.scalar(text("SELECT operator_id FROM auth_recovery_operators WHERE login_role='c3_operator'"
                                    " AND revoked_at IS NULL"))
    operator_id = existing or uuid.uuid4()
    provision_operator(w.engine, login="c3_operator", operator_id=operator_id)
    cases = Path(tempfile.mkdtemp(prefix="ras-c3-case-"))
    ns = SimpleNamespace(engine=w.engine, path=w.path, settings=w.settings, journal_id=journal.read(w.path).journal_id,
                         operator_id=operator_id, cases=cases)
    engine = create_engine(url, hide_parameters=True)
    try:
        hold(ns)
        result = operate(ns, make_case(ns, uid, ops.RESOLVE_STATUS), engine=engine)
        recover(ns)
        return result
    finally:
        engine.dispose()
        shutil.rmtree(cases, ignore_errors=True)


def no_leak(*engines):
    for e in engines:
        assert e.pool.checkedout() == 0


# --- authority and DB boundary ----------------------------------------------------------------------------------

def test_real_operator_set_credential_commits_receipt_and_new_login(world, monkeypatch):
    w = world
    uid = account(w)
    old_session = live_session(w, uid, monkeypatch)
    from app.identity import password_reset as reset
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    with Session(w.runtime) as db:
        token = reset.request_reset(db, email=scalar(w, "SELECT email FROM users WHERE id=:u", u=uid), now=NOW,
                                    lifetime=w.settings.auth.password_reset, clock=lambda: NOW)
    monkeypatch.undo()
    quarantine(w, uid)
    hold(w)
    assert user(w, uid).credential_state == "quarantined"
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    before = user(w, uid)
    result = operate(w, c)
    after = user(w, uid)
    assert (result.credential_state, result.status, result.status_safety, result.account_live) == ("usable", "active", "proven", True)
    assert (after.role, after.status, after.status_safety) == (before.role, before.status, before.status_safety)
    assert after.credential_operation_id == result.operation_id and after.credential_generation == result.generation
    assert verify_password(after.password_verifier, NEW) and not verify_password(after.password_verifier, SECRET)
    with Session(w.engine) as db:
        receipt = db.get(AuthChangeReceipt, result.operation_id)
        assert (receipt.reason_code, receipt.domain, receipt.user_id) == ("verified_recovery", "credential", uid)
        mirrors = db.execute(text("SELECT kind,journal_confirmed,case_id FROM revocation_journal_records"
                                  " WHERE seq > (SELECT max(seq)-4 FROM revocation_journal_records) ORDER BY seq")).all()
        assert [m.kind for m in mirrors] == ["operator_recovery_case", "credential_transition", "reset_token_closure",
                                             "session_revocation"] and all(m.journal_confirmed for m in mirrors)
        assert db.get(UserSession, old_session).revoked_reason == "operator_recovery"
        assert db.get(PasswordResetToken, token.token_id).closure_reason == "operator_recovery"
        audit = db.execute(text("SELECT details FROM audit_events WHERE action='auth.operator_recovery.credential'")).scalar_one()
        assert audit["case_id"] == str(c.case_id) and audit["operator_id"] == str(w.operator_id)
        assert set(audit) == {"case_id", "operator_id", "operation_id", "method", "tokens_revoked", "sessions_revoked"}
    raw = w.path.read_text(encoding="ascii")
    assert NEW not in raw and after.password_verifier not in raw and "example.test" not in raw
    no_leak(w.operator)
    recover(w)  # close the hold; safety stays pending
    assert recovery.evaluate(w.settings, w.runtime).reason == "auth_safety_pending"
    with pytest.raises(InvalidCredentials):
        sign_in(w, uid, SECRET, monkeypatch)
    issued = sign_in(w, uid, NEW, monkeypatch)
    with Session(w.runtime) as db:
        assert session_live(db, issued.session_id, NOW, w.settings.auth.session_idle, user_id=uid)
        assert not session_live(db, old_session, NOW, w.settings.auth.session_idle, user_id=uid)
        assert not reset.token_live(db, token.token_id, uid, NOW)


@pytest.mark.parametrize("who", ["runtime", "recovery", "owner"])
def test_non_operator_logins_cannot_use_operator_boundary(world, who):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    engine = {"runtime": w.runtime, "recovery": w.recovery, "owner": w.engine}[who]
    with engine.connect() as conn, pytest.raises(DBAPIError):
        conn.execute(text("SELECT ras_auth_operator_whoami()"))
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(ops.OperatorRefused, match="operator_authority_required"):
        operate(w, c, engine=engine)
    assert (counts(w), w.path.read_bytes()) == before


def test_membership_set_role_and_unmapped_grants_never_impersonate(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    member_url = ensure_login(w, "c3_member", "RAS_C3_OPERATOR2_PASSWORD")
    member = create_engine(member_url, hide_parameters=True)
    try:
        with w.engine.begin() as conn:
            conn.execute(text("GRANT c3_operator TO c3_member"))
        with member.connect() as conn:
            conn.execute(text("SET ROLE c3_operator"))
            assert conn.scalar(text("SELECT current_user")) == "c3_operator"
            with pytest.raises(DBAPIError, match="operator authority required"):
                conn.execute(text("SELECT ras_auth_operator_whoami()"))
        # A membership on the operator login itself also closes it (fail closed until removed).
        with pytest.raises(ops.OperatorRefused, match="operator_authority_required"):
            operate(w, c)
        with w.engine.begin() as conn:
            conn.execute(text("REVOKE c3_operator FROM c3_member"))
            # Granted EXECUTE but never mapped: still no authority.
            conn.execute(text("GRANT EXECUTE ON FUNCTION ras_auth_operator_whoami() TO c3_member"))
        with member.connect() as conn, pytest.raises(DBAPIError, match="operator authority required"):
            conn.execute(text("SELECT ras_auth_operator_whoami()"))
        with pytest.raises(ValueError, match="mapping conflict"):
            provision_operator(w.engine, login="c3_member", operator_id=w.operator_id)
        assert operate(w, c).credential_state == "usable"
    finally:
        with w.engine.begin() as conn:
            conn.execute(text("REVOKE c3_operator FROM c3_member"))
            conn.execute(text("REVOKE ALL ON FUNCTION ras_auth_operator_whoami() FROM c3_member"))
        member.dispose()


def test_provisioning_refuses_runtime_privileged_and_conflicting_logins(world):
    w = world
    for login_name in ("c1_runtime", "c1_recovery"):
        with pytest.raises(ValueError):
            provision_operator(w.engine, login=login_name, operator_id=uuid.uuid4())
    with pytest.raises(ValueError, match="distinct"):
        provision_operator(w.engine, login="c1_definer", operator_id=uuid.uuid4())
    with pytest.raises(ValueError, match="mapping conflict"):
        provision_operator(w.engine, login="c3_operator", operator_id=uuid.uuid4())
    provision_operator(w.engine, login="c3_operator", operator_id=w.operator_id)  # idempotent
    with w.engine.connect() as conn:
        for routine in ("ras_auth_change(uuid,uuid,text,integer)", "ras_auth_mirror(text,text,text,boolean)",
                        "ras_auth_confirm(bigint)", "ras_auth_closure_mirror(text,text,text)",
                        "ras_auth_restrict(uuid)", "ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)"):
            assert not conn.scalar(text("SELECT has_function_privilege('c3_operator',:f,'EXECUTE')"), {"f": routine})
        for table in ("users", "auth_change_receipts", "revocation_journal_records", "auth_recovery_cases",
                      "auth_recovery_operators", "sessions", "audit_events"):
            assert not conn.scalar(text("SELECT has_table_privilege('c3_operator',:t,'SELECT,INSERT,UPDATE,DELETE')"),
                                   {"t": table}), table
        assert not conn.scalar(text("SELECT has_database_privilege('c3_operator',current_database(),'CREATE,TEMP')"))
        for role in ("c1_runtime", "c1_recovery"):
            assert not conn.scalar(text("SELECT has_function_privilege(:r,'ras_auth_operator_stage(uuid,timestamptz,text[],text,integer)','EXECUTE')"),
                                   {"r": role})
    # Re-provisioning the runtime boundary keeps operator grants and never maps runtime/recovery.
    provision(w.engine, runtime="c1_runtime", recovery="c1_recovery", definer="c1_definer")
    with w.engine.connect() as conn:
        assert conn.scalar(text("SELECT has_function_privilege('c3_operator','ras_auth_operator_whoami()','EXECUTE')"))
        owner = conn.scalar(text("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname='ras_auth_operator_stage'"))
        assert owner == "c1_definer"


def test_revoked_operator_and_registry_rows_are_final(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    for engine in (w.runtime, w.recovery):
        with engine.begin() as conn, pytest.raises(DBAPIError):
            conn.execute(text("INSERT INTO auth_recovery_cases SELECT * FROM auth_recovery_cases LIMIT 1"))
    for sql in ("UPDATE auth_recovery_cases SET expires_at=expires_at+interval '1 day'", "DELETE FROM auth_recovery_cases",
                "UPDATE auth_recovery_operators SET login_role='c3_operator2'", "DELETE FROM auth_recovery_operators"):
        with w.engine.begin() as conn, pytest.raises(DBAPIError):
            conn.execute(text(sql))
    revoke_operator(w.engine, operator_id=w.operator_id, now=datetime.now(UTC))
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(ops.OperatorRefused, match="operator_authority_required"):
        operate(w, c)
    assert (counts(w), w.path.read_bytes()) == before
    with pytest.raises(ValueError, match="mapping conflict"):
        provision_operator(w.engine, login="c3_operator", operator_id=w.operator_id)


def raw_record(seq, kind, body, prev):
    """Bypass the Python parser on purpose: exercise the independent SQL checks."""
    digest = journal._digest(seq, kind, body, prev)
    return digest, journal.canonical({"body": body, "hash": digest, "kind": kind, "prev": prev, "seq": seq})


def stage_lines(w, c, mutate=None, at=NOW):
    state = journal.read(w.path)
    case_body = {"case_id": str(c.case_id), "operator_id": str(c.operator_id), "user_id": str(c.user_id),
                 "action": c.action, "operation_id": str(uuid.uuid4()), "recorded_at": at.isoformat()}
    event = {"user_id": str(c.user_id), "operation_id": case_body["operation_id"], "generation": str(uuid.uuid4()),
             "reason_code": "verified_recovery", "recorded_at": at.isoformat()}
    if mutate:
        mutate(case_body, event)
    first_hash, first = raw_record(state.head.seq + 1, journal.OPERATOR_RECOVERY_CASE, case_body, state.head.hash)
    _, second = raw_record(state.head.seq + 2, journal.CREDENTIAL_TRANSITION, event, first_hash)
    return [first, second]


@pytest.mark.parametrize("shape", ["other_user", "other_action", "other_operator", "time", "operation", "null_user",
                                   "extra_field", "reason", "valid"])
def test_direct_sql_stage_binds_case_user_action_time_and_operation(world, shape):
    w = world
    uid, other = account(w), account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    def corrupt(case_body, event):
        if shape == "other_user":
            case_body["user_id"] = event["user_id"] = str(other)
        elif shape == "other_action":
            case_body["action"] = "resolve_initial_status"
        elif shape == "other_operator":
            case_body["operator_id"] = str(w.operator2_id)
        elif shape == "time":
            case_body["recorded_at"] = event["recorded_at"] = (NOW + timedelta(seconds=1)).isoformat()
        elif shape == "operation":
            event["operation_id"] = str(uuid.uuid4())
        elif shape == "null_user":
            case_body["user_id"] = None
        elif shape == "extra_field":
            case_body["verified"] = "true"
        elif shape == "reason":
            event["reason_code"] = "password_reset"
    lines = stage_lines(w, c, corrupt)
    before = counts(w)
    with w.operator.connect() as conn:
        trans = conn.begin()
        conn.execute(text("SELECT * FROM ras_auth_operator_snapshot(:c,:at,true)"), {"c": c.case_id, "at": NOW})
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": recovery.JOURNAL_LOCK})
        call = text("SELECT ras_auth_operator_stage(:c,:at,CAST(:l AS text[]),:v,1)")
        params = {"c": c.case_id, "at": NOW, "l": lines, "v": hash_password(NEW)}
        if shape == "valid":
            conn.execute(call, params)
            row = conn.execute(text("SELECT * FROM ras_auth_operator_outcome(:c)"), {"c": c.case_id}).one()
            assert row.receipt and row.current_pointer and not row.case_confirmed
        else:
            with pytest.raises(DBAPIError):
                conn.execute(call, params)
        trans.rollback()
    assert counts(w) == before


def test_generic_paths_cannot_mint_operator_reasons(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    recover(w)
    for engine, reason, domain in ((w.runtime, "verified_recovery", "credential"),
                                   (w.runtime, "verified_initial_status", "status"),
                                   (w.engine, "verified_recovery", "credential")):
        change = (Change(uid, "credential", reason, NOW, verifier=hash_password(NEW), policy_version=1) if domain == "credential"
                  else Change(uid, "status", reason, NOW, decision="active"))
        before = (counts(w), w.path.read_bytes())
        with Session(engine) as db, pytest.raises(DBAPIError, match="operator recovery receipt needs its case use"):
            db.execute(text("SELECT ras_auth_lock_users(:ids)"), {"ids": [uid]})
            recovery.record_auth_endings(db, w.path, [], [], auth_changes=[change])
        assert (counts(w), w.path.read_bytes()) == before  # refused before the first journal byte
    # Recovery has raw INSERT on the mirror, but a live case row needs the mapped operator login.
    state = journal.read(w.path)
    body = {"case_id": str(uuid.uuid4()), "operator_id": str(w.operator_id), "user_id": str(uid),
            "action": "set_credential", "operation_id": str(uuid.uuid4()), "recorded_at": NOW.isoformat()}
    record = journal.make_record(state.head.seq + 1, journal.OPERATOR_RECOVERY_CASE, body, state.head.hash)
    with Session(w.recovery) as db, pytest.raises(DBAPIError, match="not authorised for this login"):
        recovery.insert_case_mirror(db, record, "close", "case_used", confirmed=False)
    # Generic reasons still work for existing writers.
    journal_changes(w, uid, [Change(uid, "credential", "password_reset", NOW, verifier=hash_password(NEW), policy_version=1)])
    assert user(w, uid).credential_state == "usable"


# --- T07-T10 ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["user", "admin"])
def test_quarantine_active_proven_recovers_only_credential(world, role, monkeypatch):
    w = world
    uid = account(w, role)
    quarantine(w, uid)
    hold(w)
    result = operate(w, make_case(w, uid, ops.SET_CREDENTIAL))
    assert result.account_live and user(w, uid).role == role
    recover(w)
    assert sign_in(w, uid, NEW, monkeypatch).session_id


def test_held_stays_held_until_separate_status_case(world, monkeypatch):
    w = world
    uid = account(w, "admin")
    quarantine(w, uid)
    hold_status(w, uid)
    hold(w)
    u = user(w, uid)
    assert (u.credential_state, u.status, u.status_safety) == ("quarantined", "active", "held")
    cred = operate(w, make_case(w, uid, ops.SET_CREDENTIAL))
    assert (cred.credential_state, cred.status_safety, cred.account_live) == ("usable", "held", False)
    # The credential case cannot be re-used as a status decision and vice versa.
    status_case = make_case(w, uid, ops.RESOLVE_STATUS)
    result = operate(w, status_case)
    assert (result.status, result.status_safety, result.account_live) == ("active", "proven", True)
    with Session(w.engine) as db:
        assert db.get(AuthChangeReceipt, result.operation_id).reason_code == "verified_initial_status"
        assert db.get(User, uid).credential_operation_id == cred.operation_id
    recover(w)
    assert sign_in(w, uid, NEW, monkeypatch).session_id
    # C2b enable/disable accepts this operator initial receipt as initial-status evidence.
    with Session(w.engine) as db:
        from app.identity.users import _initial_status_verified
        assert _initial_status_verified(db, uid)


def test_disabled_never_enabled_and_latest_disabled_refused(world, monkeypatch):
    w = world
    uid = account(w, "admin")
    journal_changes(w, uid, [Change(uid, "status", "admin_disable", NOW, decision="disabled")])
    quarantine(w, uid)
    hold(w)
    result = operate(w, make_case(w, uid, ops.SET_CREDENTIAL))
    assert (result.credential_state, result.status, result.account_live) == ("usable", "disabled", False)
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(ops.OperatorRefused, match="^status_disabled$"):
        operate(w, make_case(w, uid, ops.RESOLVE_STATUS))
    assert (counts(w), w.path.read_bytes())[1] == before[1]
    recover(w)
    with pytest.raises(InvalidCredentials):
        sign_in(w, uid, NEW, monkeypatch)


def test_disabled_snapshot_with_later_enable_intent_stays_disabled(world):
    """Business disabled + latest head an enable intent without receipt: only the business guard applies."""
    w = world
    uid = account(w)
    journal_changes(w, uid, [Change(uid, "status", "admin_disable", NOW, decision="disabled")])
    journal_changes(w, uid, [Change(uid, "status", "admin_enable", NOW, decision="active")], rollback=True)
    hold(w)
    u = user(w, uid)
    assert (u.status, u.status_safety) == ("disabled", "held")
    _, status = ops.journal_heads(journal.read(w.path), uid)
    assert status.body["decision"] == "active"
    c = make_case(w, uid, ops.RESOLVE_STATUS)
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(ops.OperatorRefused, match="^status_disabled$"):
        operate(w, c)
    assert (counts(w), w.path.read_bytes()) == before
    assert user(w, uid).status == "disabled"


def test_latest_journal_disable_with_active_snapshot_is_refused(world):
    w = world
    uid = account(w)
    # Disable journaled but its DB transaction rolled back: snapshot says active, latest decision is disabled.
    journal_changes(w, uid, [Change(uid, "status", "admin_disable", NOW, decision="disabled")], rollback=True)
    hold(w)
    assert user(w, uid).status == "disabled"  # replay applied the restriction
    with pytest.raises(ops.OperatorRefused, match="status_disabled|latest_status_disabled"):
        operate(w, make_case(w, uid, ops.RESOLVE_STATUS))


def test_all_admins_quarantined_and_legacy_only_admin(world, monkeypatch):
    w = world
    admins = [account(w, "admin"), account(w, "admin")]
    for uid in admins:
        quarantine(w, uid)
    legacy = account(w, "admin", live=False)  # no receipt/generation: legacy quarantined/held after recover
    hold(w)
    for uid in admins:
        assert operate(w, make_case(w, uid, ops.SET_CREDENTIAL)).account_live
    u = user(w, legacy)
    assert (u.credential_state, u.status_safety, u.credential_generation) == ("quarantined", "held", None)
    c = make_case(w, legacy, ops.SET_CREDENTIAL)
    assert c.expected_credential_operation_id is None and c.expected_status_operation_id is None
    assert not operate(w, c).account_live
    assert operate(w, make_case(w, legacy, ops.RESOLVE_STATUS)).account_live
    with pytest.raises(ops.OperatorRefused, match="status_already_proven"):
        operate(w, make_case(w, legacy, ops.RESOLVE_STATUS))
    assert scalar(w, "SELECT count(*) FROM users") == 3  # no replacement/bootstrap account
    recover(w)
    assert sign_in(w, legacy, NEW, monkeypatch).session_id


def test_only_admin_disabled_stays_locked_and_bootstrap_refused(world, monkeypatch):
    from app.identity.bootstrap import BootstrapRefused, bootstrap_admin
    from app.config import PasswordPolicy
    w = world
    uid = account(w, "admin", live=False, status="disabled")
    hold(w)  # backfill journals legacy_disabled
    assert heads(w, uid)[1] is not None
    assert not operate(w, make_case(w, uid, ops.SET_CREDENTIAL)).account_live
    with pytest.raises(ops.OperatorRefused, match="status_disabled"):
        operate(w, make_case(w, uid, ops.RESOLVE_STATUS))
    recover(w)
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    with pytest.raises(BootstrapRefused):
        bootstrap_admin(w.runtime, email="replacement@example.test", password=NEW, policy=PasswordPolicy(), clock=lambda: NOW)
    assert user(w, uid).status == "disabled"


def test_absent_user_and_same_email_other_uuid_refused(world, monkeypatch):
    w = world
    gone = uuid.uuid4()
    other = account(w, email="shared-marker@example.test")
    hold(w)
    absent = make_case(w, gone, ops.SET_CREDENTIAL, role="user")
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(ops.OperatorRefused, match="user_absent"):
        operate(w, absent)
    # A case for one UUID cannot be pointed at another User with the same email.
    wrong = make_case(w, other, ops.SET_CREDENTIAL)
    with pytest.raises(ops.OperatorRefused, match="case_target_mismatch"):
        ops.run(w.operator, w.settings, wrong, user_id=gone, installation=w.journal_id, action=wrong.action,
                clock=lambda: NOW, read_password=lambda: NEW)
    assert (counts(w), w.path.read_bytes()) == before


def test_bootstrap_singleton_is_only_supporting_evidence(world, monkeypatch):
    from app.config import PasswordPolicy
    from app.identity.bootstrap import BootstrapRefused, bootstrap_admin
    w = world
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    first = bootstrap_admin(w.runtime, email="bootstrap-marker@example.test", password=SECRET, policy=PasswordPolicy(),
                            clock=lambda: NOW).user_id
    monkeypatch.undo()
    second = account(w, "admin")
    for uid in (first, second):
        quarantine(w, uid)
    hold(w)
    assert ops.inspect(w.operator, w.settings, first)["bootstrap_singleton"] == "yes"
    assert ops.inspect(w.operator, w.settings, second)["bootstrap_singleton"] == "other"
    # The singleton neither authorises nor blocks: the case binds the UUID; bootstrap is never a rescue.
    assert operate(w, make_case(w, second, ops.SET_CREDENTIAL)).account_live
    assert operate(w, make_case(w, first, ops.SET_CREDENTIAL)).account_live
    recover(w)
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    with pytest.raises(BootstrapRefused):
        bootstrap_admin(w.runtime, email="again@example.test", password=NEW, policy=PasswordPolicy(), clock=lambda: NOW)
    assert scalar(w, "SELECT count(*) FROM users WHERE role='admin'") == 2


@pytest.mark.parametrize("problem", ["expired", "not_yet", "lifetime", "unregistered", "file_mismatch", "other_operator",
                                     "wrong_installation", "stale_credential", "stale_status", "role", "used_twice",
                                     "wrong_action"])
def test_case_binding_refusals_write_nothing(world, problem):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    kw = {}
    if problem == "expired":
        kw = dict(approved=NOW - timedelta(hours=2), expires=NOW - timedelta(seconds=1))
    elif problem == "not_yet":
        kw = dict(approved=NOW + timedelta(minutes=1), expires=NOW + timedelta(hours=1))
    elif problem == "other_operator":
        kw = dict(operator=w.operator2_id)
    elif problem == "stale_credential":
        kw = dict(expected_credential_operation_id=str(uuid.uuid4()))
    elif problem == "stale_status":
        kw = dict(expected_status_operation_id=None)
    elif problem == "role":
        kw = dict(role="admin")
    if problem == "lifetime":
        c = make_case(w, uid, ops.SET_CREDENTIAL, register=False, approved=NOW - timedelta(minutes=5),
                      expires=NOW + timedelta(days=2))
        with pytest.raises(ValueError, match="window outside policy"):
            register_case(w.engine, c, now=NOW, max_lifetime=w.settings.auth_recovery.case_max_lifetime)
        with Session(w.engine) as db:  # labelled trusted-provisioner bypass of the policy check
            db.execute(text("INSERT INTO auth_recovery_cases(case_id,action,operator_id,approver_id,journal_id,user_id,"
                            "expected_role,expected_credential_operation_id,expected_status_operation_id,verification_method,"
                            "approved_at,expires_at) VALUES(:a,:b,:c,:d,:e,:f,:g,:h,:i,:j,:k,:l)"),
                       dict(zip("abcdefghijkl", (c.case_id, c.action, c.operator_id, c.approver_id, c.journal_id, c.user_id,
                            c.expected_role, c.expected_credential_operation_id, c.expected_status_operation_id,
                            c.verification_method, c.approved_at, c.expires_at))))
            db.commit()
    elif problem in ("not_yet",):
        c = make_case(w, uid, ops.SET_CREDENTIAL, **kw)
    elif problem == "expired":
        c = make_case(w, uid, ops.SET_CREDENTIAL, register=False, **kw)
        # Registered while still valid; it expired before use.
        register_case(w.engine, c, now=NOW - timedelta(hours=1), max_lifetime=w.settings.auth_recovery.case_max_lifetime)
    elif problem == "unregistered":
        c = make_case(w, uid, ops.SET_CREDENTIAL, register=False)
    elif problem == "file_mismatch":
        registered = make_case(w, uid, ops.SET_CREDENTIAL)
        c = parse_case(json.dumps({**json.loads((w.cases / f"{registered.case_id}.json").read_text()),
                                   "verification_method": "video_call"}).encode())
    elif problem == "wrong_installation":
        c = make_case(w, uid, ops.SET_CREDENTIAL)
        before = (counts(w), w.path.read_bytes())
        with pytest.raises(ops.OperatorRefused, match="case_target_mismatch"):
            ops.run(w.operator, w.settings, c, user_id=uid, installation=uuid.uuid4(), action=c.action,
                    clock=lambda: NOW, read_password=lambda: NEW)
        assert (counts(w), w.path.read_bytes()) == before
        return
    elif problem == "wrong_action":
        c = make_case(w, uid, ops.SET_CREDENTIAL)
        with pytest.raises(ops.OperatorRefused, match="case_target_mismatch"):
            ops.run(w.operator, w.settings, c, user_id=uid, installation=w.journal_id, action=ops.RESOLVE_STATUS,
                    clock=lambda: NOW, read_password=lambda: NEW)
        return
    elif problem == "used_twice":
        c = make_case(w, uid, ops.SET_CREDENTIAL)
        operate(w, c)
    else:
        c = make_case(w, uid, ops.SET_CREDENTIAL, **kw)
    before = (counts(w), w.path.read_bytes())
    expected = {"expired": "case_expired", "not_yet": "case_not_yet_valid", "lifetime": "case_lifetime_exceeds_policy",
                "unregistered": "case_not_registered", "file_mismatch": "case_mismatch",
                "other_operator": "case_operator_mismatch", "stale_credential": "case_stale_credential_head",
                "stale_status": "case_stale_status_head", "role": "role_mismatch", "used_twice": "case_already_used"}[problem]
    with pytest.raises(ops.OperatorRefused, match=expected):
        operate(w, c)
    assert (counts(w), w.path.read_bytes()) == before
    no_leak(w.operator)


@pytest.mark.parametrize("password", ["SECRET", "email", "short", "blank"])
def test_password_policy_and_snapshot_reuse_refused_before_db_write(world, password):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    email = scalar(w, "SELECT email FROM users WHERE id=:u", u=uid)
    value = {"SECRET": SECRET, "email": email.upper(), "short": "short", "blank": " " * 20}[password]
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(PasswordPolicyError):
        operate(w, make_case(w, uid, ops.SET_CREDENTIAL), password=value)
    after = (counts(w), w.path.read_bytes())
    assert after[1] == before[1] and after[0]["auth_change_receipts"] == before[0]["auth_change_receipts"]


# --- races, clocks and release ----------------------------------------------------------------------------------

def test_no_db_session_or_lock_held_across_prompt_and_hash(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    engine = create_engine(w.operator_url, poolclass=NullPool, hide_parameters=True)
    seen = []
    def prompt():
        seen.append(scalar(w, "SELECT count(*) FROM pg_stat_activity WHERE usename='c3_operator' AND datname=current_database()"))
        return NEW
    try:
        result = ops.run(engine, w.settings, c, user_id=uid, installation=w.journal_id, action=c.action,
                         clock=lambda: NOW, read_password=prompt)
    finally:
        engine.dispose()
    assert seen == [0] and result.account_live
    assert scalar(w, "SELECT count(*) FROM pg_stat_activity WHERE usename='c3_operator' AND datname=current_database()") == 0


def test_expiry_during_hash_or_lock_wait_refuses_without_writes(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL, expires=NOW + timedelta(minutes=1))
    now = [NOW]
    def slow_prompt():
        now[0] = NOW + timedelta(minutes=2)  # hashing/typing outlives the case window
        return NEW
    before = (counts(w), w.path.read_bytes())
    with pytest.raises(ops.OperatorRefused, match="case_expired"):
        ops.run(w.operator, w.settings, c, user_id=uid, installation=w.journal_id, action=c.action,
                clock=lambda: now[0], read_password=slow_prompt)
    assert (counts(w), w.path.read_bytes()) == before
    # Waiting on a real User row lock: the clock read after the wait decides.
    now[0] = NOW
    blocker = w.engine.connect()
    tx = blocker.begin()
    blocker.execute(text("SELECT 1 FROM users WHERE id=:u FOR NO KEY UPDATE"), {"u": uid})
    errors = []
    def run():
        try:
            ops.run(w.operator, w.settings, c, user_id=uid, installation=w.journal_id, action=c.action,
                    clock=lambda: now[0], read_password=lambda: NEW)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    t = threading.Thread(target=run)
    t.start()
    for _ in range(200):
        if scalar(w, "SELECT count(*) FROM pg_stat_activity WHERE usename='c3_operator' AND wait_event_type='Lock'"):
            break
        threading.Event().wait(0.05)
    else:
        raise AssertionError("operator never waited on the User lock")
    now[0] = NOW + timedelta(minutes=2)
    tx.rollback(); blocker.close()
    t.join(30)
    assert len(errors) == 1 and getattr(errors[0], "code", None) == "case_expired"
    assert (counts(w), w.path.read_bytes()) == before
    no_leak(w.operator, w.engine)


def test_expiry_during_fsync_rolls_back_after_journal_and_replay_only_restricts(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL, expires=NOW + timedelta(minutes=1))
    now = [NOW]
    def slow_append(path, state, records):
        result = journal.append(path, state, records)
        now[0] = NOW + timedelta(minutes=2)  # fsync returned after the window
        return result
    receipts = counts(w)["auth_change_receipts"]
    with pytest.raises(ops.OperatorOutcomeError, match="rolled_back_after_journal") as err:
        operate(w, c, clock=lambda: now[0], append=slow_append)
    assert err.value.committed is False
    assert counts(w)["auth_change_receipts"] == receipts
    assert not recovery.evaluate_integrity(w.settings, w.runtime).open
    recover(w, hold_for_operator=True)
    u = user(w, uid)
    assert u.credential_state == "quarantined" and verify_password(u.password_verifier, SECRET)
    assert counts(w)["auth_change_receipts"] == receipts  # replay never creates a receipt
    with pytest.raises(ops.OperatorRefused, match="case_already_used"):
        operate(w, c)
    fresh = make_case(w, uid, ops.SET_CREDENTIAL)  # the head moved: a new decision is required
    assert fresh.expected_credential_operation_id != c.expected_credential_operation_id
    assert operate(w, fresh).account_live


@pytest.mark.parametrize("change", ["role", "operator_revoked", "other_operator_credential", "verifier"])
def test_change_between_proof_and_lock_refuses(world, change):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    other = make_case(w, uid, ops.SET_CREDENTIAL, operator=w.operator2_id)
    def prompt():
        if change == "role":
            with w.engine.begin() as conn:
                conn.execute(text("UPDATE users SET role='admin' WHERE id=:u"), {"u": uid})
        elif change == "operator_revoked":
            revoke_operator(w.engine, operator_id=w.operator_id, now=datetime.now(UTC))
        elif change == "other_operator_credential":
            operate(w, other, engine=w.operator2, password=NEW + "x")
        elif change == "verifier":
            with w.engine.begin() as conn:  # labelled owner rewrite of the snapshot verifier (triggers bypassed)
                conn.execute(text("SET LOCAL session_replication_role = replica"))
                conn.execute(text("UPDATE users SET password_verifier=:v WHERE id=:u"), {"v": hash_password("x" * 20), "u": uid})
        return NEW
    journal_before = w.path.read_bytes()
    with pytest.raises(ops.OperatorRefused) as err:
        ops.run(w.operator, w.settings, c, user_id=uid, installation=w.journal_id, action=c.action,
                clock=lambda: NOW, read_password=prompt)
    assert err.value.code == ("operator_authority_required" if change == "operator_revoked" else "target_changed")
    if change != "other_operator_credential":
        assert w.path.read_bytes() == journal_before
    no_leak(w.operator, w.operator2)


def test_two_operators_same_target_concurrently_one_decision(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    cases = [make_case(w, uid, ops.SET_CREDENTIAL), make_case(w, uid, ops.SET_CREDENTIAL, operator=w.operator2_id)]
    barrier = threading.Barrier(2)
    results = []
    def worker(case, engine, password):
        def prompt():
            barrier.wait(10)
            return password
        try:
            results.append(ops.run(engine, w.settings, case, user_id=uid, installation=w.journal_id, action=case.action,
                                   clock=lambda: NOW, read_password=prompt))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
    threads = [threading.Thread(target=worker, args=(cases[0], w.operator, NEW)),
               threading.Thread(target=worker, args=(cases[1], w.operator2, NEW + "-second"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    wins = [r for r in results if isinstance(r, ops.OperatorResult)]
    losses = [r for r in results if isinstance(r, ops.OperatorRefused)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].code in ("target_changed", "case_stale_credential_head", "target_busy", "journal_lock_busy")
    assert scalar(w, "SELECT count(*) FROM auth_change_receipts WHERE reason_code='verified_recovery'") == 1
    no_leak(w.operator, w.operator2)


def test_rollback_failure_and_lost_commit_reply_never_success(world, monkeypatch):
    from sqlalchemy.engine.base import RootTransaction
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    real_commit = RootTransaction.commit
    def lost_reply(self):
        real_commit(self)
        raise ConnectionResetError("reply lost")
    monkeypatch.setattr(RootTransaction, "commit", lost_reply)
    with pytest.raises(ops.OperatorOutcomeError, match="auth_outcome_uncertain") as err:
        operate(w, c)
    monkeypatch.undo()
    assert err.value.committed is None
    out = ops.inspect(w.operator, w.settings, uid, c.case_id)
    assert out["case_outcome"] == "committed" and out["case_receipt"] and out["case_current"]
    with pytest.raises(ops.OperatorRefused, match="case_already_used"):  # no resubmission
        operate(w, c)
    no_leak(w.operator)
    # A failing rollback while refusing still returns/invalidates the connection.
    other = account(w, live=False)
    recover(w, hold_for_operator=True)
    bad = make_case(w, other, ops.SET_CREDENTIAL, expires=NOW + timedelta(minutes=1))
    from sqlalchemy.engine.base import Connection
    real_rollback = Connection.rollback
    calls = []
    def failing(self):
        calls.append(1)
        raise ConnectionResetError("rollback lost")
    monkeypatch.setattr(RootTransaction, "rollback", failing)
    monkeypatch.setattr(Connection, "rollback", failing)
    try:
        with pytest.raises(ops.OperatorRefused, match="case_expired"):
            operate(w, bad, clock=lambda: NOW + timedelta(minutes=2))
    finally:
        monkeypatch.setattr(Connection, "rollback", real_rollback)
        monkeypatch.undo()
    assert calls and w.operator.pool.checkedout() == 0


# --- journal fault matrix, replay and restore -------------------------------------------------------------------

def fault_append(kind, *, fail_with=OSError):
    """none / torn first line / complete prefix (fsynced) / complete batch whose fsync failed."""
    def append(path, state, records):
        data = [r.line.encode("ascii") + b"\n" for r in records]
        with open(path, "ab") as handle:
            if kind == "torn":
                handle.write(data[0][:-7])
            elif kind == "prefix":
                handle.write(data[0])
            elif kind == "full":
                handle.write(b"".join(data))
            handle.flush()
            if kind == "prefix":
                os.fsync(handle.fileno())
        raise fail_with("journal write failed")
    return append


@pytest.mark.parametrize("fault", ["none", "torn", "prefix", "full"])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "uncertain"])
def test_t15_fault_matrix(world, monkeypatch, fault, outcome):
    from sqlalchemy.engine.base import RootTransaction
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    receipts = counts(w)["auth_change_receipts"]
    if outcome == "uncertain":
        real = RootTransaction.commit
        def lost(self):
            real(self)
            raise ConnectionResetError("reply lost")
        monkeypatch.setattr(RootTransaction, "commit", lost)
    append = fault_append(fault, fail_with=RuntimeError if outcome == "rollback" else OSError)
    with pytest.raises(ops.OperatorOutcomeError) as err:
        operate(w, c, append=append)
    monkeypatch.undo()
    assert err.value.code == {"commit": "auth_journal_unavailable", "rollback": "rolled_back_after_journal",
                              "uncertain": "auth_outcome_uncertain"}[outcome]
    committed = outcome != "rollback"
    if committed:
        # The whole batch committed unconfirmed: no complete prefix is ever confirmed after an append failure.
        assert scalar(w, "SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed") == 2
        assert counts(w)["auth_change_receipts"] == receipts + 1
    else:
        assert counts(w)["auth_change_receipts"] == receipts
    reason = recovery.evaluate_integrity(w.settings, w.engine).reason
    assert reason == {("commit", "none"): "journal_behind_db", ("commit", "torn"): "journal_torn",
                      ("commit", "prefix"): "journal_behind_db", ("commit", "full"): "journal_unconfirmed",
                      ("rollback", "none"): "recovery_in_progress", ("rollback", "torn"): "journal_torn",
                      ("rollback", "prefix"): "db_behind_journal", ("rollback", "full"): "db_behind_journal",
                      }[("commit" if committed else "rollback", fault)]
    # A live DB ahead of its journal needs the explicit confirmation; journal-ahead only replays.
    if committed and fault != "full":
        with pytest.raises(recovery.RecoveryError):
            recover(w, hold_for_operator=True)
        recover(w, hold_for_operator=True, confirm_db_current=True)
    else:
        recover(w, hold_for_operator=True)
    recover(w, hold_for_operator=True)  # idempotent
    u = user(w, uid)
    state = journal.read(w.path)
    assert [r.seq for r in state.records] == list(range(1, len(state.records) + 1))
    assert scalar(w, "SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed") == 0
    used = ops.case_used(state, c.case_id)
    if committed:
        assert u.credential_state == "usable" and used and verify_password(u.password_verifier, NEW)
        assert counts(w)["auth_change_receipts"] == receipts + 1
    else:
        assert u.credential_state == "quarantined" and verify_password(u.password_verifier, SECRET)
        assert counts(w)["auth_change_receipts"] == receipts  # replay never creates a receipt
        assert used == (fault in ("prefix", "full"))
        if used:
            with pytest.raises(ops.OperatorRefused, match="case_already_used"):
                operate(w, c)
        else:
            assert operate(w, c).account_live  # nothing durable: the same case is still unused


def dump_restore(w, admin_engine, tmp_path, label):
    import subprocess
    from tests.conftest import create_empty_database
    name = os.environ["RAS_C3_TEST_CONTAINER"]
    details = json.loads(subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True).stdout)[0]
    assert details["Config"]["Labels"].get("ras.task") == os.environ["RAS_C3_TEST_PROJECT"]
    dump = subprocess.run(["docker", "exec", name, "pg_dump", "-U", "task_admin", "-Fc", "-d", w.engine.url.database],
                          check=True, capture_output=True).stdout
    assert dump.startswith(b"PGDMP")
    def restore():
        url = create_empty_database(admin_engine)
        subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", "task_admin", "-d", url.database],
                       input=dump, check=True, capture_output=True)
        return url
    return restore


def test_two_dump_restore_rounds_never_reuse_case_or_rebuild_credential(world, admin_engine, tmp_path):
    from tests.conftest import drop_database
    w = world
    uid = account(w, "admin")
    other = account(w)
    quarantine(w, uid)
    hold(w)
    restore = dump_restore(w, admin_engine, tmp_path, "before-g3")  # snapshot: uid quarantined, no C3 receipt
    first = make_case(w, uid, ops.SET_CREDENTIAL)
    lost = [operate(w, first)]
    used = [first]
    source = w.path.read_bytes()
    current = source
    for round_no in range(2):
        url = restore()
        copy = tmp_path / f"round-{round_no}" / "journal.jsonl"
        copy.parent.mkdir()
        copy.write_bytes(current)  # the CURRENT independent journal, never part of the dump
        settings = w.settings.model_copy(deep=True)
        settings.recovery.journal_path = copy
        owner = create_engine(url, hide_parameters=True)
        op_engine = create_engine(url.set(username="c3_operator", password=os.environ["RAS_C3_OPERATOR_PASSWORD"]),
                                  hide_parameters=True)
        try:
            recovery.hold(owner, settings, NOW)
            for _ in range(2):
                recovery.recover(owner, SimpleNamespace(), settings, NOW, hold_for_operator=True)
            with Session(owner) as db:
                u = db.get(User, uid)
                assert u.credential_state == "quarantined" and verify_password(u.password_verifier, SECRET)
                for result in lost:
                    assert u.credential_operation_id != result.operation_id
                    assert db.get(AuthChangeReceipt, result.operation_id) is None  # never rebuilt by replay
                assert db.get(User, other).credential_state == "usable"  # an untouched account keeps working
            for case in used:  # consumed per the current journal, though the restored DB has no use of it
                with pytest.raises(ops.OperatorRefused, match="case_already_used"):
                    ops.run(op_engine, settings, case, user_id=uid, installation=w.journal_id, action=case.action,
                            clock=lambda: NOW, read_password=lambda: NEW)
            credential, _ = ops.journal_heads(journal.read(copy), uid)
            body = json.loads((w.cases / f"{first.case_id}.json").read_text())
            body.update(case_id=str(uuid.uuid4()), expected_credential_operation_id=credential.body["operation_id"])
            path = w.cases / f"{body['case_id']}.json"
            path.write_text(json.dumps(body))
            fresh = load_case_file(path, max_bytes=4096)
            register_case(owner, fresh, now=NOW - timedelta(minutes=1), max_lifetime=settings.auth_recovery.case_max_lifetime)
            result = ops.run(op_engine, settings, fresh, user_id=uid, installation=w.journal_id, action=fresh.action,
                             clock=lambda: NOW, read_password=lambda: f"{NEW}-round-{round_no}")
            assert result.account_live and all(result.generation != r.generation for r in lost)
            lost.append(result)
            used.append(fresh)
            recovery.recover(owner, SimpleNamespace(), settings, NOW)
            assert recovery.evaluate(settings, owner).reason == "auth_safety_pending"
            current = copy.read_bytes()
        finally:
            op_engine.dispose()
            owner.dispose()
            drop_database(admin_engine, url)
    assert w.path.read_bytes() == source  # restores wrote only their own journal copies


# --- inspect and maintenance hold ---------------------------------------------------------------------------------

def test_inspect_reports_codes_without_secrets_and_requires_hold_for_mutation(world):
    w = world
    uid = account(w)
    quarantine(w, uid)
    recover(w)  # reconciled, hold closed
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    out = ops.inspect(w.operator, w.settings, uid)
    assert (out["gate"], out["maintenance_hold"]) == ("open", "gate_open")
    assert out["credential"] == "quarantined" and out["status_proven"] and not out["account_live"]
    text_out = json.dumps({k: str(v) for k, v in out.items()})
    assert "example.test" not in text_out and "argon2" not in text_out
    with pytest.raises(ops.OperatorRefused, match="gate_open"):
        operate(w, c)
    # A hold written to the journal only (not reconciled into the DB) is not a maintenance hold.
    recovery.hold(w.engine, w.settings, NOW)
    with pytest.raises(ops.OperatorRefused, match="gate_db_behind_journal"):
        operate(w, c)
    # An interrupted recover (begin mirrored, progress not advanced) is not reconciled either.
    def broken(*a, **k):
        raise recovery.RecoveryError("injected")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(recovery, "_import_legacy_auth_tokens", broken)
        with pytest.raises(recovery.RecoveryError):
            recover(w, hold_for_operator=True)
    with pytest.raises(ops.OperatorRefused, match="hold_not_reconciled"):
        operate(w, c)
    recover(w, hold_for_operator=True)
    assert ops.inspect(w.operator, w.settings, uid)["maintenance_hold"] == "ready"
    assert operate(w, c).account_live


@pytest.mark.parametrize("damage", ["torn", "missing", "unconfirmed"])
def test_hold_never_excuses_damaged_or_unconfirmed_chain(world, damage):
    w = world
    uid = account(w)
    quarantine(w, uid)
    hold(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL)
    if damage == "torn":
        with open(w.path, "ab") as handle:
            handle.write(b'{"body":')
    elif damage == "missing":
        w.path.rename(w.path.with_name("moved.jsonl"))
    else:
        with w.engine.begin() as conn:  # labelled owner corruption: a confirmed mirror cannot go back to false
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text("UPDATE revocation_journal_records SET journal_confirmed=false WHERE seq=(SELECT max(seq) FROM revocation_journal_records)"))
    with pytest.raises(ops.OperatorRefused, match="gate_"):
        operate(w, c)


def test_case_file_strict_schema_and_location(world, tmp_path):
    w = world
    uid = account(w)
    c = make_case(w, uid, ops.SET_CREDENTIAL, register=False)
    raw = json.loads((w.cases / f"{c.case_id}.json").read_text())
    for bad in ({**raw, "verified": True}, {k: v for k, v in raw.items() if k != "approver_id"},
                {**raw, "user_id": raw["user_id"].upper()}, {**raw, "case_id": "00000000-0000-1000-8000-000000000001"},
                {**raw, "expected_role": "owner"}, {**raw, "approved_at": "2026-10-03T09:00:00"},
                {**raw, "expires_at": raw["approved_at"]}, {**raw, "action": "enable"}, {**raw, "schema": "v0"}):
        with pytest.raises(CaseError):
            parse_case(json.dumps(bad).encode())
    with pytest.raises(CaseError):
        parse_case(b'{"case_id":"a","case_id":"b"}')
    from pathlib import Path
    inside = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pytest.raises(CaseError, match="case_file_inside_repo"):
        load_case_file(inside, max_bytes=4096)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * 5000)
    with pytest.raises(CaseError, match="too_large"):
        load_case_file(big, max_bytes=4096)
