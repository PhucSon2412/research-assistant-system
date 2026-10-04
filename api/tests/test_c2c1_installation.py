"""The runtime SQL creation boundary requires a bound new-install genesis."""
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import auth_safety, journal, recovery
from app.config import get_settings
from app.identity.login import login
from app.identity.session_commit import AUTH_PENDING, commit_auth
from tests.conftest import upgrade
from tests.test_auth_safety_foundation import NOW, runtime_engine
from tests.test_c2c1_bootstrap import (
    EMAIL, PASSWORD, counts, initial, installation, prepare, world,
)


def snapshot(w):
    # Include exact mirrors and all audit rows, not only account counts.
    with w.runtime.connect() as conn:
        return {t: conn.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all()
                for t in ("users", "bootstrap_state", "auth_change_receipts", "auth_safety_heads",
                          "audit_events", "recovery_registrations", "revocation_journal_records")}


def assert_sql_refused(w):
    before = snapshot(w)
    file_before = w.path.read_bytes()
    with Session(w.runtime) as db:
        with pytest.raises(DBAPIError, match="bootstrap requires bound new installation"):
            # Deliberately no bootstrap wrapper, Python history guard or entry gate.
            prepare(w, db)
        db.rollback()
    assert snapshot(w) == before
    assert w.path.read_bytes() == file_before


def test_direct_existing_registration_recovered_refused(empty_db_url, journal_path, app_env, monkeypatch):
    upgrade(empty_db_url)
    app_env(empty_db_url)
    engine = create_engine(empty_db_url, hide_parameters=True)
    w = SimpleNamespace(engine=engine, path=journal_path, settings=get_settings())
    try:
        recovery.register_existing(engine, w.settings, NOW)
        recovery.recover(engine, SimpleNamespace(), w.settings, NOW)
        w.runtime = runtime_engine(w)
        app_env(w.runtime.url)
        w.settings = get_settings()
        monkeypatch.setattr(auth_safety, "cutover", lambda db: "ready")
        assert recovery.evaluate_integrity(w.settings, w.runtime).open
        assert journal.read(w.path).records[0].body["mode"] == "existing_db"
        assert_sql_refused(w)
    finally:
        if hasattr(w, "runtime"):
            w.runtime.dispose()
        engine.dispose()


@pytest.mark.parametrize("shape", [
    "registration_missing", "registration_mode_null", "registration_existing",
    "registration_adopted", "registration_rebuilt", "registration_journal_id_null",
    "registration_hash_null", "registration_journal_id_mismatch", "registration_hash_mismatch",
    "genesis_missing", "genesis_mode_missing", "genesis_mode_null", "genesis_existing",
    "genesis_rebuilt", "genesis_journal_id_missing", "genesis_journal_id_null",
    "genesis_journal_id_mismatch", "genesis_hash_missing", "genesis_hash_null",
    "genesis_hash_mismatch", "genesis_kind_mismatch", "genesis_seq_mismatch",
    "genesis_prev_mismatch", "genesis_unconfirmed",
])
def test_direct_malformed_registration_genesis_refused(world, shape):
    w = world
    # Setup only: a migrator creates otherwise unreachable malformed snapshots.
    # Runtime still executes the actual SECURITY DEFINER routine with real grants.
    with w.engine.begin() as conn:
        conn.execute(text("ALTER TABLE recovery_registrations DISABLE TRIGGER USER"))
        conn.execute(text("ALTER TABLE revocation_journal_records DISABLE TRIGGER USER"))
        if shape == "registration_missing":
            conn.execute(text("DELETE FROM recovery_registrations"))
        elif shape.startswith("registration_"):
            field = ("journal_id" if "journal_id" in shape else
                     "genesis_hash" if "hash" in shape else "mode")
            if shape.endswith("null"):
                conn.execute(text(f"ALTER TABLE recovery_registrations ALTER COLUMN {field} DROP NOT NULL"))
                value = None
            elif field == "journal_id":
                value = uuid.uuid4()
            elif field == "genesis_hash":
                value = "a" * 64
            else:
                value = {"registration_existing": "existing_db", "registration_adopted": "adopted",
                         "registration_rebuilt": "rebuilt_from_db"}[shape]
            conn.execute(text(f"UPDATE recovery_registrations SET {field}=:value"), {"value": value})
        elif shape == "genesis_missing":
            conn.execute(text("DELETE FROM revocation_journal_records WHERE seq=1"))
        elif shape == "genesis_unconfirmed":
            conn.execute(text("UPDATE revocation_journal_records SET journal_confirmed=false WHERE seq=1"))
        else:
            line = json.loads(conn.scalar(text("SELECT line FROM revocation_journal_records WHERE seq=1")))
            field = "mode" if "mode" in shape or shape in ("genesis_existing", "genesis_rebuilt") else "journal_id"
            if shape.endswith("missing"):
                if "hash" in shape: del line["hash"]
                else: del line["body"][field]
            elif shape.endswith("null"):
                if "hash" in shape: line["hash"] = None
                else: line["body"][field] = None
            elif shape == "genesis_hash_mismatch": line["hash"] = "a" * 64
            elif shape == "genesis_kind_mismatch": line["kind"] = "recovery_begin"
            elif shape == "genesis_seq_mismatch": line["seq"] = 2
            elif shape == "genesis_prev_mismatch": line["prev"] = "a" * 64
            elif shape == "genesis_existing": line["body"]["mode"] = "existing_db"
            elif shape == "genesis_rebuilt": line["body"]["mode"] = "rebuilt_from_db"
            else: line["body"]["journal_id"] = str(uuid.uuid4())
            conn.execute(text("UPDATE revocation_journal_records SET line=:line WHERE seq=1"),
                         {"line": journal.canonical(line)})
        conn.execute(text("ALTER TABLE recovery_registrations ENABLE TRIGGER USER"))
        conn.execute(text("ALTER TABLE revocation_journal_records ENABLE TRIGGER USER"))
    assert_sql_refused(w)


def test_direct_bound_new_install_initial_pair_and_login(world):
    w = world
    with Session(w.runtime) as db:
        uid, params = prepare(w, db)
        db.info[AUTH_PENDING] = initial(uid, params)
        commit_auth(db)
    assert counts(w)["users"] == counts(w)["bootstrap_state"] == 1
    assert counts(w)["auth_change_receipts"] == 2
    with Session(w.runtime) as db:
        assert auth_safety.account_live(db, uid)
        issued = login(db, email=EMAIL, password=PASSWORD, now=NOW,
                       settings=w.settings, clock=lambda: NOW)
        assert issued[1].session_id
    assert recovery.evaluate_integrity(w.settings, w.runtime).open
