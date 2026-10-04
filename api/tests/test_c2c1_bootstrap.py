"""Runtime LOGIN bootstrap; only cutover result is replaced in scoped fixtures."""
import io
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery
from app.api.errors import ApiError
from app.config import get_settings
from app.identity import bootstrap
from app.identity.bootstrap import BootstrapRefused
from app.identity.passwords import hash_password, verify_password
from app.identity.session_commit import AUTH_PENDING, commit_auth
from app.models import User, BootstrapState, AuthChangeReceipt
from tests.conftest import API_DIR, db_env
from tests.helpers import DEV_ENV
from tests.test_auth_safety_foundation import runtime_engine, NOW, run_recover

PASSWORD = "C2c1-private-password-marker-456!"
EMAIL = "bootstrap-private-marker@example.test"
PREPARE = text("SELECT ras_auth_bootstrap_prepare(:uid,:email,:name,:v,:p,:at)")


@pytest.fixture
def installation(engine, app_env, migrated_db_url, journal_path):
    app_env(migrated_db_url)
    w = SimpleNamespace(engine=engine, path=journal_path, settings=get_settings())
    w.runtime = runtime_engine(w)
    app_env(w.runtime.url)
    w.settings = get_settings()
    try:
        yield w
    finally:
        w.runtime.dispose()


@pytest.fixture
def world(installation, monkeypatch):
    monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
    return installation


def create(w, email=EMAIL):
    result = bootstrap.bootstrap_admin(w.runtime, email=email, password=PASSWORD,
                                      policy=w.settings.password, clock=lambda: NOW)
    w.uid = result.user_id
    return result


def prepare(w, db, **kw):
    params = {"uid": uuid.uuid4(), "email": EMAIL, "name": None,
              "v": hash_password(PASSWORD), "p": 1, "at": NOW} | kw
    db.execute(PREPARE, params)
    return params["uid"], params


def initial(uid, params):
    return [auth_safety.Change(uid, "credential", "bootstrap", params["at"],
                              verifier=params["v"], policy_version=params["p"]),
            auth_safety.Change(uid, "status", "bootstrap", params["at"], decision="active")]


def counts(w):
    with w.runtime.connect() as conn:
        return {t: conn.scalar(text(f"SELECT count(*) FROM {t}")) for t in
                ("users", "bootstrap_state", "auth_change_receipts", "audit_events", "sessions")}


def test_runtime_initial_pair_live_login_and_c2b_initial_status(world, caplog):
    from app.identity.login import login
    from app.identity.users import _initial_status_verified
    w = world
    result = create(w, " Bootstrap-Private-Marker@Example.Test ")
    assert counts(w) == {"users": 1, "bootstrap_state": 1, "auth_change_receipts": 2, "audit_events": 1, "sessions": 0}
    with Session(w.runtime) as db:
        u = db.get(User, result.user_id)
        verifier = u.password_verifier
        assert verify_password(verifier, PASSWORD) and u.email == EMAIL
        assert auth_safety.account_live(db, u.id) and _initial_status_verified(db, u.id)
        receipts = list(db.scalars(select(AuthChangeReceipt)))
        singleton = db.get(BootstrapState, True)
        assert {r.domain for r in receipts} == {"credential", "status"}
        assert all(r.transaction_id == singleton.creation_transaction_id and r.recorded_at == NOW
                   and r.reason_code == "bootstrap" for r in receipts)
        audit = " ".join(db.execute(text("SELECT row_to_json(a)::text FROM audit_events a")).scalars())
    state = journal.read(w.path)
    assert [r.kind for r in state.records[1:]] == list(journal.AUTH_KINDS)
    for marker in (PASSWORD, verifier, EMAIL):
        assert marker not in w.path.read_text() + caplog.text + audit + repr(result)
    with Session(w.runtime) as db:
        issued = login(db, email=EMAIL, password=PASSWORD, now=NOW, settings=w.settings, clock=lambda: NOW)
        assert issued[1].session_id
    assert recovery.evaluate_integrity(w.settings, w.runtime).open


def test_once_and_concurrent_barrier(world):
    w = world
    barrier = threading.Barrier(4)
    def attempt(i):
        barrier.wait(15)
        try:
            return create(w, f"admin{i}@example.test").user_id
        except BootstrapRefused:
            return None
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(r is not None for r in results) == 1
    assert counts(w)["users"] == counts(w)["bootstrap_state"] == 1
    before = w.path.read_bytes()
    with pytest.raises(BootstrapRefused): create(w)
    assert w.path.read_bytes() == before


@pytest.mark.parametrize("query", [
    "INSERT INTO users(email,password_verifier) VALUES ('x@example.test','$argon2id$fake')",
    "DELETE FROM bootstrap_state", "UPDATE bootstrap_state SET admin_user_id=gen_random_uuid()",
    "INSERT INTO bootstrap_state(admin_user_id) VALUES(gen_random_uuid())",
    "UPDATE users SET status='active'", "DELETE FROM auth_change_receipts",
    "UPDATE revocation_journal_records SET journal_confirmed=true",
    "CREATE TEMP TABLE bypass(id int)", "SET ROLE c1_definer",
])
def test_runtime_raw_writes_denied(world, query):
    create(world)
    with world.runtime.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text(query))


def test_singleton_immutable_even_sql_owner_and_recovery_cannot_create(world):
    w = world; create(w)
    for query in ("DELETE FROM bootstrap_state", "UPDATE bootstrap_state SET completed_at=now()",
                  "UPDATE bootstrap_state SET admin_user_id=gen_random_uuid()"):
        with w.engine.begin() as conn, pytest.raises(DBAPIError, match="bootstrap singleton is final"):
            conn.execute(text(query))
    with w.runtime.connect() as conn:
        for role in ("c1_runtime", "c1_recovery"):
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert not conn.scalar(text("SELECT has_table_privilege(:role,'bootstrap_state',:privilege)"),
                                       {"role": role, "privilege": privilege})
        assert not conn.scalar(text("SELECT has_function_privilege('c1_recovery',"
            "'ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)','EXECUTE')"))
        assert not conn.scalar(text("SELECT has_table_privilege('c1_recovery','bootstrap_state','INSERT')"))
        row = conn.execute(text("SELECT r.rolcanlogin,p.proconfig FROM pg_proc p JOIN pg_roles r ON p.proowner=r.oid"
                                " WHERE p.proname='ras_auth_bootstrap_prepare'")).one()
        assert not row[0] and "search_path=pg_catalog, public, pg_temp" in row[1]


@pytest.mark.parametrize("kw", [
    {"uid": None}, {"uid": uuid.UUID(int=1)}, {"email": None}, {"email": "BAD@EXAMPLE.TEST"},
    {"email": "bad email@example.test"}, {"v": None}, {"v": "private-phc-marker"},
    {"p": None}, {"p": 99}, {"at": None},
])
def test_direct_creation_invalid_input_no_orphan(world, kw):
    w = world; before = w.path.read_bytes()
    with Session(w.runtime) as db:
        with pytest.raises(DBAPIError): prepare(w, db, **kw)
        db.rollback()
    assert counts(w)["users"] == 0 and w.path.read_bytes() == before


@pytest.mark.parametrize("shape", ["none", "credential", "status", "wrong_reason", "wrong_time", "wrong_user", "intent_only"])
def test_pair_guard_before_first_byte_and_commit(world, monkeypatch, shape):
    w = world; before = w.path.read_bytes(); append = []
    monkeypatch.setattr(journal, "append", lambda *args: append.append(args))
    with Session(w.runtime) as db:
        uid, params = prepare(w, db)
        values = initial(uid, params)
        if shape == "credential": values = values[:1]
        elif shape == "status": values = values[1:]
        elif shape == "wrong_reason": values[-1] = replace(values[-1], reason_code="admin_enable")
        elif shape == "wrong_time": values[-1] = replace(values[-1], recorded_at=NOW + timedelta(seconds=1))
        elif shape == "wrong_user": values[-1] = replace(values[-1], user_id=uuid.uuid4())
        if shape in ("none", "intent_only"):
            if shape == "intent_only":
                record = journal.next_record(journal.read(w.path).head, journal.CREDENTIAL_TRANSITION,
                    {"user_id": str(uid), "operation_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()),
                     "reason_code": "bootstrap", "recorded_at": NOW.isoformat()})
                db.execute(text("SELECT ras_auth_mirror(:line,'close','auth_changed',false)"), {"line": record.line})
            with pytest.raises(DBAPIError, match="bootstrap needs its initial receipt pair"):
                db.commit()
        else:
            db.info[AUTH_PENDING] = values
            with pytest.raises(ApiError): commit_auth(db)
        db.rollback()
    assert not append and w.path.read_bytes() == before and counts(w)["users"] == 0


@pytest.mark.parametrize("shape", ["none", "torn", "prefix", "full", "ok"])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "uncertain_commit", "uncertain_rollback"])
def test_fault_matrix_writer_receipts_replay_retry(world, monkeypatch, shape, outcome):
    w = world; real_append = journal.append; real_commit = Session.commit
    def fault(path, state, records):
        # Mirrors, receipts, singleton, User and audit all staged before first byte.
        assert len(records) == 2 and records[0].kind == journal.CREDENTIAL_TRANSITION
        if shape == "ok": return real_append(path, state, records)
        if shape == "torn":
            with path.open("ab") as stream: stream.write(records[0].line[:25].encode()); stream.flush()
        elif shape == "prefix": real_append(path, state, records[:1])
        elif shape == "full":
            with monkeypatch.context() as patch:
                patch.setattr(journal.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic fsync")))
                real_append(path, state, records)
        raise OSError("synthetic append")
    def commit(db):
        if outcome in ("commit", "uncertain_commit"): real_commit(db)
        else: db.rollback()
        if outcome != "commit": raise OSError("synthetic lost reply")
    with monkeypatch.context() as patch:
        patch.setattr(journal, "append", fault)
        patch.setattr(Session, "commit", commit)
        if shape == "ok" and outcome == "commit": create(w)
        else:
            with pytest.raises(ApiError, match="auth_journal_unavailable" if outcome == "commit" else "auth_outcome_uncertain"):
                create(w)
    landed = outcome in ("commit", "uncertain_commit")
    assert counts(w) == {"users": int(landed), "bootstrap_state": int(landed),
                         "auth_change_receipts": 2 * int(landed), "audit_events": int(landed), "sessions": 0}
    with Session(w.runtime) as db:
        assert db.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed")) == (2 if landed and shape != "ok" else 0)
    if landed or shape != "none":
        run_recover(w, confirm_db_current=landed)
        after = w.path.read_bytes(); run_recover(w)
        # Recover appends begin/end, but never creates account/receipts/singleton.
        assert counts(w)["users"] == counts(w)["bootstrap_state"] == int(landed)
        assert counts(w)["auth_change_receipts"] == 2 * int(landed)
        if landed or shape in ("prefix", "full", "ok"):
            with pytest.raises(BootstrapRefused): create(w)
        else: create(w)  # torn with no complete intent is not account history.
        assert len(after) > 0
    else:
        create(w)  # no journal bytes + known rollback permits a fresh UUID attempt.


@pytest.mark.parametrize("shape", ["credential", "pair"])
def test_snapshot_absent_bootstrap_intent_blocks_after_recover_twice(world, shape):
    w = world
    with Session(w.runtime) as db:
        uid, params = prepare(w, db)
        values = initial(uid, params)
        records = auth_safety.stage(db, journal.read(w.path), values)
        state = journal.read(w.path)
        journal.append(w.path, state, records[:1] if shape == "credential" else records)
        db.rollback()  # snapshot from before creation, independent file retained.
    assert counts(w)["users"] == 0
    for _ in range(2): run_recover(w)
    assert {k: v for k, v in counts(w).items() if k != "audit_events"} == {
        "users": 0, "bootstrap_state": 0, "auth_change_receipts": 0, "sessions": 0}
    with w.runtime.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM audit_events WHERE action='admin.bootstrap'")) == 0
    with pytest.raises(BootstrapRefused): create(w)
    with Session(w.runtime) as db, pytest.raises(DBAPIError, match="bootstrap already completed"):
        prepare(w, db)


@pytest.mark.parametrize("change", ["hold", "missing", "corrupt", "intent"])
def test_hash_outside_transaction_rechecks_real_integrity(world, monkeypatch, change):
    w = world; real = bootstrap.hasher_for_policy
    def factory(policy):
        hasher = real(policy)
        class Paused:
            def hash(self, password):
                assert w.runtime.pool.checkedout() == 0
                if change == "hold": recovery.hold(w.engine, w.settings, NOW)
                elif change == "missing": w.path.unlink()
                elif change == "corrupt": w.path.write_bytes(b"corrupt\n")
                else:
                    state = journal.read(w.path)
                    body = {"user_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()),
                            "reason_code": "bootstrap", "recorded_at": NOW.isoformat()}
                    journal.append(w.path, state, [journal.next_record(state.head, journal.CREDENTIAL_TRANSITION, body)])
                return hasher.hash(password)
        return Paused()
    monkeypatch.setattr(bootstrap, "hasher_for_policy", factory)
    with pytest.raises(ApiError, match="service_recovering"): create(w)
    assert counts(w)["users"] == 0


def test_waiting_bootstrap_lock_rechecks_hold_with_observed_advisory_wait(world, monkeypatch):
    w = world; entered = threading.Event(); release = threading.Event(); pid = []
    real = bootstrap.hasher_for_policy
    def factory(policy):
        hasher = real(policy)
        class Paused:
            def hash(self, password):
                result = hasher.hash(password); entered.set(); assert release.wait(15); return result
        return Paused()
    monkeypatch.setattr(bootstrap, "hasher_for_policy", factory)
    with w.runtime.connect() as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": bootstrap._ADVISORY_LOCK_KEY})
        job = pool.submit(create, w); assert entered.wait(15); release.set()
        try:
            with w.engine.connect() as observer:
                for _ in range(300):
                    waiting = observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE"
                        " datname=current_database() AND usename='c1_runtime' AND wait_event='advisory')"))
                    if waiting: break
                    observer.rollback(); threading.Event().wait(.005)
                else: raise AssertionError("bootstrap never waited for advisory lock")
            recovery.hold(w.engine, w.settings, NOW)
        finally:
            blocker.rollback()
        with pytest.raises(ApiError, match="service_recovering"): job.result(20)
    assert counts(w)["users"] == 0


@pytest.mark.parametrize("change", ["hold", "new_intent"])
def test_final_integrity_and_exact_pair_after_known_commit(world, monkeypatch, change):
    w = world; real = bootstrap.commit_auth
    def commit(db):
        batch = real(db)
        if change == "hold": recovery.hold(w.engine, w.settings, NOW)
        else:
            # Journal can advance after known commit; replay must not invent a receipt.
            uid = uuid.UUID(batch[0].record.body["user_id"])
            state = journal.read(w.path)
            event = {"user_id": str(uid), "operation_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()),
                     "reason_code": "bootstrap", "recorded_at": NOW.isoformat()}
            journal.append(w.path, state, [journal.next_record(state.head, journal.CREDENTIAL_TRANSITION, event)])
        return batch
    monkeypatch.setattr(bootstrap, "commit_auth", commit)
    with pytest.raises(ApiError, match="service_recovering"): create(w)
    assert counts(w)["users"] == 1 and counts(w)["sessions"] == 0


def test_mirrors_and_pair_all_false_before_one_batch_append(world, monkeypatch):
    w = world; real = recovery.record_auth_endings; append = journal.append; deciding = []
    def capture(db, *args, **kw):
        deciding.append(db)
        return real(db, *args, **kw)
    def checked(path, state, records):
        db = deciding[0]
        assert db.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed")) == 2
        assert db.scalar(text("SELECT count(*) FROM auth_change_receipts")) == 2
        assert db.scalar(text("SELECT count(*) FROM bootstrap_state")) == 1
        assert db.scalar(text("SELECT count(*) FROM audit_events WHERE action='admin.bootstrap'")) == 1
        with w.engine.connect() as other:
            assert other.scalar(text("SELECT count(*) FROM users")) == 0
        return append(path, state, records)
    monkeypatch.setattr(recovery, "record_auth_endings", capture)
    monkeypatch.setattr(journal, "append", checked)
    create(w)
    assert len(deciding) == 1


@pytest.mark.parametrize("shared", [False, True])
def test_prepare_rejects_row_locks_after_journal(world, shared):
    with Session(world.runtime) as db:
        lock = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        db.execute(text(f"SELECT {lock}(:k)"), {"k": recovery.JOURNAL_LOCK})
        with pytest.raises(DBAPIError, match="bootstrap must precede journal lock"):
            prepare(world, db)
        db.rollback()


@pytest.mark.parametrize("kind", journal.AUTH_KINDS)
def test_independent_history_guard_complete_prefix_without_any_db_mirror(installation, kind):
    w = installation; state = journal.read(w.path)
    body = {"user_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()),
            "reason_code": "bootstrap", "recorded_at": NOW.isoformat()}
    if kind == journal.ACCOUNT_STATUS_DECISION: body["decision"] = "active"
    journal.append(w.path, state, [journal.next_record(state.head, kind, body)])
    assert not bootstrap._history_is_new(journal.read(w.path))
    # Gate/pending remains real; also exercise the history refusal without assuming
    # a DB mirror or singleton. No writer is run through a bypassed integrity gate.
    with Session(w.runtime) as db, pytest.raises(BootstrapRefused): bootstrap._require_new(db)
    assert counts(w)["users"] == counts(w)["bootstrap_state"] == 0


@pytest.mark.parametrize("mode", ["existing_db", "rebuilt_from_db"])
def test_non_new_genesis_never_proves_initial_installation(installation, mode):
    state = journal.read(installation.path)
    body = dict(state.records[0].body); body["mode"] = mode
    genesis = journal.make_record(1, journal.GENESIS, body, journal.ZERO_HASH)
    assert not bootstrap._history_is_new(SimpleNamespace(records=[genesis]))


def cli_process(w, args=(), *, scoped=False, stdin=PASSWORD + "\n"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAS_")}
    env.update(DEV_ENV); env.update(db_env(w.runtime.url))
    env["RAS_RECOVERY__JOURNAL_PATH"] = str(w.path)
    command = [sys.executable, "-m", "app.cli.bootstrap_admin"]
    if scoped:
        # Test-only process scope replaces ONLY pending boundary, real CLI/module.
        command = [sys.executable, "-c", "import runpy; from app import auth_safety; auth_safety.cutover=lambda db:'ready'; runpy.run_module('app.cli.bootstrap_admin',run_name='__main__')"]
    return subprocess.run(command + ["--email", EMAIL, *args], input=stdin, capture_output=True,
                          text=True, cwd=API_DIR, env=env, timeout=45)


def test_real_cli_runtime_stdin_and_unpatched_pending(installation):
    w = installation
    pending = cli_process(w, ["--password-stdin"])
    assert pending.returncode == 1 and counts(w)["users"] == 0
    first = cli_process(w, ["--password-stdin"], scoped=True)
    assert first.returncode == 0, first.stderr
    second = cli_process(w, ["--password-stdin"], scoped=True)
    assert second.returncode == 3
    for proc in (pending, first, second):
        assert PASSWORD not in proc.stdout + proc.stderr and EMAIL not in proc.stdout + proc.stderr
    assert counts(w)["sessions"] == 0


@pytest.mark.parametrize("args,stdin", [([], PASSWORD), (["--password", PASSWORD], ""),
    (["--pass", PASSWORD], ""), (["--password=" + PASSWORD], ""), (["--password-stdin=" + PASSWORD], ""),
    (["--password-stdin"], ""), (["--password-stdin"], "weak"), (["--password-stdin"], "x" * 1025)])
def test_cli_invalid_secret_inputs(installation, args, stdin):
    proc = cli_process(installation, args, stdin=stdin)
    assert proc.returncode == 2
    assert PASSWORD not in proc.stdout + proc.stderr
    assert counts(installation)["users"] == 0
