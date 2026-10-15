"""B1: SQL writer and historical consumer provenance, using real runtime LOGIN."""
from dataclasses import replace
import json
import os
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery, operator_recovery as ops
from app.api.errors import ApiError
from app.config import get_settings
from app.db_roles import provision_operator, provision
from app.identity.bootstrap import bootstrap_admin
from app.identity.invitations import create_invitation, accept_invitation
from app.identity.login import login
from app.identity.passwords import hash_password
from app.identity.users import set_user_status
from app.models import User
from app.models.enums import UserStatus
from tests.conftest import upgrade, register_new_install, alembic_config, create_empty_database, drop_database
from tests.test_c3_operator_recovery import ensure_login
from app.identity.users import _initial_status_verified
from tests.test_auth_safety_foundation import (
    foundation, runtime, append_changes, changes, run_recover, user, runtime_engine, NOW, SECRET,
)


@pytest.mark.parametrize("reason", ["bootstrap", "invitation_accept"])
@pytest.mark.parametrize("domain", ["credential", "status"])
def test_runtime_cannot_mint_initial_receipt_for_existing_user(foundation, runtime, reason, domain):
    w = foundation
    before = user(w)
    data = w.path.read_bytes()
    value = next(c for c in changes(w) if c.domain == domain)
    with pytest.raises(DBAPIError):
        append_changes(w, [replace(value, reason_code=reason)], engine=runtime)
    after = user(w)
    assert (after.credential_operation_id, after.status_operation_id) == (
        before.credential_operation_id, before.status_operation_id)
    assert w.path.read_bytes() == data


@pytest.mark.parametrize("reason", ["bootstrap", "invitation_accept"])
def test_generic_initial_reason_is_not_enable_evidence(foundation, runtime, reason):
    w = foundation
    values = changes(w)
    values[-1] = replace(values[-1], reason_code=reason)
    # At the vulnerable base this commits a forged receipt and the real enable
    # consumer accepts it. The fixed writer must refuse before any journal byte.
    try:
        append_changes(w, values, engine=runtime)
    except DBAPIError:
        pass
    else:
        run_recover(w)
    with Session(runtime) as db:
        assert not _initial_status_verified(db, w.uid)


@pytest.fixture
def old_install(empty_db_url, journal_path, app_env, monkeypatch):
    """Real 0049 snapshot with a valid bootstrap/invitation and known forged receipts."""
    upgrade(empty_db_url, "0049")
    register_new_install(empty_db_url, journal_path)
    owner = create_engine(empty_db_url, hide_parameters=True)
    app_env(empty_db_url)
    w = SimpleNamespace(engine=owner, path=journal_path, settings=get_settings())
    w.runtime = runtime_engine(w)
    app_env(w.runtime.url); w.settings = get_settings()
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    w.uid = bootstrap_admin(w.runtime, email="valid-admin@example.test", password=SECRET,
                            policy=w.settings.password, clock=lambda: NOW).user_id
    with Session(w.runtime) as db:
        w.actor = login(db, email="valid-admin@example.test", password=SECRET, now=NOW,
                        settings=w.settings, clock=lambda: NOW)[1]
    with Session(w.runtime) as db:
        created = create_invitation(db, inviter_id=w.uid, actor_session_id=w.actor.session_id,
            csrf_token=w.actor.csrf_token, email="valid-user@example.test", display_name=None,
            now=NOW, lifetime=w.settings.auth.invite, clock=lambda: NOW)
    with Session(w.runtime) as db:
        w.invitee = accept_invitation(db, raw_token=created.token, password=SECRET, display_name=None,
            now=NOW, policy=w.settings.password, clock=lambda: NOW).id
    w.forged = []
    for reason in ("bootstrap", "invitation_accept"):
        uid = uuid.uuid4()
        with Session(owner) as db:  # labelled legacy/corruption setup, never a usable fixture
            db.add(User(id=uid, email=f"bad-{uid.hex}@example.test", password_verifier=hash_password(SECRET),
                        created_at=NOW, updated_at=NOW, password_changed_at=NOW))
            db.commit()
        bad = SimpleNamespace(**vars(w)); bad.uid = uid
        append_changes(bad, [replace(c, reason_code=reason) for c in changes(bad)], engine=w.runtime)
        w.forged.append(uid)
    with Session(w.runtime) as db:
        assert all(auth_safety.account_live(db, uid) for uid in w.forged)  # actual 0049 SQL consumer
    try:
        yield w
    finally:
        w.runtime.dispose(); owner.dispose()


def test_upgrade_preserves_bytes_but_consumers_deny_old_forgery(old_install):
    w = old_install
    with w.engine.connect() as db:
        before = {t: db.execute(text(f"SELECT row_to_json(r)::text FROM {t} r ORDER BY 1")).scalars().all()
                  for t in ("users", "auth_change_receipts", "revocation_journal_records")}
    raw = w.path.read_bytes()
    upgrade(w.engine.url)
    with w.engine.connect() as db:
        assert before == {t: db.execute(text(f"SELECT row_to_json(r)::text FROM {t} r ORDER BY 1")).scalars().all()
                          for t in before}
    assert w.path.read_bytes() == raw  # migration does no journal I/O or evidence backfill
    op_url = ensure_login(w, "c3_operator", "RAS_C3_OPERATOR_PASSWORD")
    provision_operator(w.engine, login="c3_operator", operator_id=uuid.uuid4())
    operator = create_engine(op_url, hide_parameters=True)
    try:
        for repeat in range(2):
            if repeat:
                recovery.recover(w.engine, SimpleNamespace(), w.settings, NOW)
            with Session(w.runtime) as db:
                for uid in (w.uid, w.invitee):
                    assert auth_safety.account_live(db, uid) and _initial_status_verified(db, uid)
                for uid in w.forged:
                    assert not auth_safety.account_live(db, uid)
                    assert not db.scalar(text("SELECT ras_auth_status_live(:u)"), {"u": uid})
                    assert not _initial_status_verified(db, uid)
            for uid in w.forged:
                inspected = ops.inspect(operator, w.settings, uid)
                assert not inspected["initial_status_verified"] and not inspected["account_live"]
                with Session(w.runtime) as db, pytest.raises(ApiError, match="initial_status_verification_required"):
                    set_user_status(db, user_id=uid, actor_id=w.uid, status=UserStatus.ACTIVE, now=NOW,
                        actor_session_id=w.actor.session_id, csrf_token=w.actor.csrf_token, clock=lambda: NOW)
        with Session(w.runtime) as db:
            for uid in w.forged:
                u = db.get(User, uid)
                assert (u.credential_state, u.status_safety) == ("quarantined", "held")
    finally:
        operator.dispose()


@pytest.mark.parametrize("kind",["bootstrap","invitation"])
def test_durable_creation_proof_survives_current_profile_changes(old_install,kind):
    w=old_install;upgrade(w.engine.url)
    uid=w.uid if kind=="bootstrap" else w.invitee
    with w.engine.begin() as db:  # privileged profile-change fixture, never new account creation
        before=db.execute(text("SELECT credential_operation_id,status_operation_id FROM users WHERE id=:u"),{"u":uid}).one()
        db.execute(text("UPDATE users SET email=:e,display_name='current profile',role=:role WHERE id=:u"),
                   {"u":uid,"e":f"new-{uid.hex}@example.test","role":"user" if kind=="bootstrap" else "admin"})
    with Session(w.runtime) as db:
        assert auth_safety.account_live(db,uid) and _initial_status_verified(db,uid)
        assert tuple(db.execute(text("SELECT credential_operation_id,status_operation_id FROM users WHERE id=:u"),{"u":uid}).one())==tuple(before)
    # The creation-time role/email were proved by the protected parent writer;
    # current profile/role must still be reloaded by each authorization consumer.
    report=recovery.recover(w.engine,SimpleNamespace(),w.settings,NOW)
    assert report.reconciliation_complete and not report.safety_ready
    with Session(w.runtime) as db:assert auth_safety.account_live(db,uid)


def test_two_dump_restore_rounds_keep_valid_provenance_and_restrict_forgery(old_install, admin_engine, tmp_path):
    w = old_install; upgrade(w.engine.url)
    name = os.environ["RAS_C3_TEST_CONTAINER"]
    facts = json.loads(subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True, timeout=30).stdout)[0]
    assert facts["Config"]["Labels"]["ras.task"] == os.environ["RAS_C3_TEST_PROJECT"]
    assert facts["NetworkSettings"]["Ports"]["5432/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": str(admin_engine.url.port)}]
    original = current = w.path.read_bytes()
    for iteration in range(2):
        dump = subprocess.run(["docker", "exec", name, "pg_dump", "-U", admin_engine.url.username,
            "-Fc", "-d", w.engine.url.database], check=True, capture_output=True, timeout=60).stdout
        assert dump.startswith(b"PGDMP")
        url = create_empty_database(admin_engine)
        restored = runtime_restored = None
        try:
            subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", admin_engine.url.username,
                "-d", url.database], input=dump, check=True, capture_output=True, timeout=60)
            restored = create_engine(url, hide_parameters=True)
            provision(restored,runtime="c1_runtime",recovery="c1_recovery",definer="c1_definer")
            runtime_restored=create_engine(url.set(username="c1_runtime",password=os.environ["RAS_C1_RUNTIME_PASSWORD"]),hide_parameters=True)
            copy = tmp_path / f"round-{iteration}.jsonl"; copy.write_bytes(current)
            settings = w.settings.model_copy(deep=True); settings.recovery.journal_path = copy
            recovery.hold(restored, settings, NOW)
            for _ in range(2):
                report = recovery.recover(restored, SimpleNamespace(), settings, NOW)
                assert report.reconciliation_complete and not report.safety_ready
                with Session(runtime_restored) as db:
                    from app.ingest.runtime import require_runtime
                    require_runtime(db)
                    for uid in (w.uid, w.invitee):
                        assert auth_safety.account_live(db, uid) and _initial_status_verified(db, uid)
                    for uid in w.forged:
                        u = db.get(User, uid)
                        assert (u.credential_state, u.status_safety) == ("quarantined", "held")
                        assert not auth_safety.account_live(db, uid) and not _initial_status_verified(db, uid)
            current = copy.read_bytes()
            assert w.path.read_bytes() == original
        finally:
            if runtime_restored:runtime_restored.dispose()
            if restored: restored.dispose()
            drop_database(admin_engine, url)


def test_0050_pristine_roundtrip(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), "0049")
    upgrade(empty_db_url)


def test_reprovision_keeps_valid_auth_lifecycles(old_install):
    from app.identity.password_reset import request_reset, confirm_reset
    w = old_install; upgrade(w.engine.url)
    for _ in range(2):
        provision(w.engine,runtime="c1_runtime",recovery="c1_recovery",definer="c1_definer")
    with Session(w.runtime) as db:
        assert auth_safety.account_live(db,w.invitee)
        issued = request_reset(db,email="valid-user@example.test",now=NOW,lifetime=w.settings.auth.password_reset,clock=lambda:NOW)
        assert issued is not None
    password="B1-reprovision-new-private-password-987!"
    with Session(w.runtime) as db:
        assert confirm_reset(db,raw_token=issued.token,new_password=password,now=NOW,policy=w.settings.password,clock=lambda:NOW)==w.invitee
    with Session(w.runtime) as db:
        assert login(db,email="valid-user@example.test",password=password,now=NOW,settings=w.settings,clock=lambda:NOW)[1].session_id
        assert not db.scalar(text("SELECT has_table_privilege(current_user,'users','UPDATE') OR has_table_privilege(current_user,'auth_change_receipts','INSERT')"))


@pytest.mark.parametrize("reason",["bootstrap","invitation_accept"])
@pytest.mark.parametrize("domain",["credential","status"])
def test_runtime_cannot_reuse_a_committed_creation_parent(old_install,reason,domain):
    w=old_install;upgrade(w.engine.url)
    target=SimpleNamespace(**vars(w));target.uid=w.invitee
    before=user(target);raw=w.path.read_bytes()
    value=next(c for c in changes(target) if c.domain==domain)
    with pytest.raises(DBAPIError):
        append_changes(target,[replace(value,reason_code=reason)],engine=w.runtime)
    after=user(target)
    assert (after.credential_operation_id,after.status_operation_id)==(before.credential_operation_id,before.status_operation_id)
    assert w.path.read_bytes()==raw


def test_old_forgery_cannot_enable_held_user_after_disable(old_install):
    w=old_install;upgrade(w.engine.url);recovery.recover(w.engine,SimpleNamespace(),w.settings,NOW)
    uid=w.forged[1]
    with Session(w.runtime) as db:
        assert db.get(User,uid).status_safety=="held"
        set_user_status(db,user_id=uid,actor_id=w.uid,status=UserStatus.DISABLED,now=NOW,
                        actor_session_id=w.actor.session_id,csrf_token=w.actor.csrf_token,clock=lambda:NOW)
    raw=w.path.read_bytes()
    with Session(w.runtime) as db,pytest.raises(ApiError,match="initial_status_verification_required"):
        set_user_status(db,user_id=uid,actor_id=w.uid,status=UserStatus.ACTIVE,now=NOW,
                        actor_session_id=w.actor.session_id,csrf_token=w.actor.csrf_token,clock=lambda:NOW)
    assert w.path.read_bytes()==raw


@pytest.mark.parametrize("owner",[False,True])
def test_drill_seed_uses_real_runtime_creation(installation,monkeypatch,owner):
    from pathlib import Path
    from app.ingest.runtime import Halt
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/"scripts"))
    from drill_accounts import seed_accounts
    w=installation;monkeypatch.setattr(auth_safety,"cutover",lambda db:"ready")
    raw=w.path.read_bytes()
    if owner:
        with pytest.raises(Halt,match="worker_runtime_required"):
            seed_accounts(w.engine,{"admin":"drill-admin@example.test","a":"drill-a@example.test"},SECRET,clock=lambda:NOW)
        assert w.path.read_bytes()==raw
    else:
        ids=seed_accounts(w.runtime,{"admin":"drill-admin@example.test","a":"drill-a@example.test"},SECRET,clock=lambda:NOW)
        with Session(w.runtime) as db:
            assert all(auth_safety.account_live(db,uid) and _initial_status_verified(db,uid) for uid in ids.values())
            assert db.scalar(text("SELECT count(*) FROM auth_change_receipts"))==4
