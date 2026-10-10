"""M6c-1 revocation journal and recovery gate (PostgreSQL + MinIO, journal file in the test's tmp dir).

A "backup" is ``CREATE DATABASE ... TEMPLATE`` of the live test database plus a
copy of every object in the test bucket; a "restore" points the app at that
database and puts the objects back, while the journal file (outside both) is
kept. Crashes are simulated by making the journal write, the DB commit or the
replay raise at the exact step; nothing kills a process. Embedding, rerank and
generation are fakes and ``httpx.HTTPTransport`` refuses outbound requests.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import uuid
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Thread
from types import SimpleNamespace

import pytest

from app.ingest import cleanup as runtime_cleanup  # normal (runtime) cleanup, not recovery
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import db as app_db
from app import journal, publication, recovery
from app.api.errors import ApiError
from app.cli import recovery as recovery_cli
from app.clock import utcnow
from app.config import get_settings
from app.ingest.worker import process_job
from app.main import app
from app.models.enums import RevocationReason
from app.recovery import RecoveryError
from tests.conftest import create_empty_database, drop_database, upgrade
from tests.test_common_rag import QUESTION, answering, ask
from tests.test_common_search import build, hit_items, search
from tests.test_publication import (MISSING, admin_principal, common_file, common_key, key_present, listed_ids,
                                    new_copy, publish, revoke, state)
from tests.test_review_requests import A_BODY, world
from tests.original_fixtures import upload_api, base_upload_api  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401

RECOVERING = (503, {"error": "service_recovering"})


def journal_file():
    return get_settings().recovery.journal_path


def gate(engine) -> str:
    status=recovery.evaluate(get_settings(),engine)
    return 'open' if status.open else status.reason


def records(kind=None) -> list[journal.Record]:
    return [r for r in journal.read(journal_file()).records if kind is None or r.kind == kind]


def mirror(engine) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT seq, kind, record_hash, line FROM revocation_journal_records ORDER BY seq")).all()]


def do_recover(engine, storage, now, **kwargs):
    return recovery.recover(engine, storage, get_settings(), now, **kwargs)


def assert_recovering(browsers, requests) -> None:
    for who, browser in browsers.items():
        for method, path, *body in requests:
            response = browser.get(path) if method == "GET" else browser.post(path, body[0])
            assert (response.status_code, response.json()) == RECOVERING, (who, method, path)
            assert response.headers["cache-control"] == "no-store"


def listed(upload_api, monkeypatch, copies: int = 1):
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch)
    items = []
    for _ in range(copies):
        copy = new_copy(api, a, admin, ids_a)
        items.append(copy)
    for copy in items[:1]:
        assert publish(admin, copy).status_code == 200
    return SimpleNamespace(api=api, storage=storage, a=a, b=b, admin=admin, ids_a=ids_a, ids_b=ids_b,
                           copy=items[0], copies=items, browsers={"a": a, "b": b, "admin": admin})


@contextmanager
def backup(api, admin_engine):
    """A snapshot of the live test database, dropped afterwards."""
    live = api.engine.url
    api.engine.dispose()
    app_db.get_engine().dispose()
    with admin_engine.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                          " WHERE datname=:d AND pid <> pg_backend_pid()"), {"d": live.database})
    url = live.set(database=f"ras_test_{uuid.uuid4().hex[:12]}")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{url.database}" TEMPLATE "{live.database}"'))
    engine = app_db.make_engine(url)
    try:
        yield SimpleNamespace(url=url, engine=engine)
    finally:
        engine.dispose()
        app_db.get_engine().dispose()
        app_db.get_engine.cache_clear()
        drop_database(admin_engine, url)


def objects(storage) -> dict[str, bytes]:
    return {key: storage.open(key).read() for key, _ in storage.orphan_keys("")}


def put_back(storage, saved: dict[str, bytes]) -> None:
    for key, data in saved.items():
        storage.put(key, io.BytesIO(data), "application/octet-stream")


def item_rows(engine, item_id: str) -> dict:
    """Every row naming the item, directly or through its file/generation (for before/after comparisons)."""
    i = uuid.UUID(item_id)
    queries = {
        "item": "SELECT * FROM library_items WHERE id=:i",
        "versions": "SELECT * FROM paper_versions WHERE item_id=:i ORDER BY id",
        "files": "SELECT * FROM source_files WHERE item_id=:i ORDER BY id",
        "chunks": "SELECT * FROM chunks WHERE item_id=:i ORDER BY id",
        "sections": "SELECT * FROM sections WHERE item_id=:i ORDER BY id",
        "generations": "SELECT * FROM index_generations WHERE item_id=:i ORDER BY id",
        "entries": "SELECT e.* FROM index_entries e JOIN index_generations g ON g.id=e.generation_id"
                   " WHERE g.item_id=:i ORDER BY e.generation_id, e.chunk_id",
        "calls": "SELECT c.* FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id"
                 " WHERE f.item_id=:i ORDER BY c.id",
        "references": "SELECT * FROM reference_entries WHERE item_id=:i ORDER BY id",
    }
    with engine.connect() as conn:
        return {name: [tuple(r) for r in conn.execute(text(sql), {"i": i}).all()] for name, sql in queries.items()}


def celery_tasks(monkeypatch):
    """The Celery task functions, called in-process; every inner call is refused if reached."""
    monkeypatch.setenv("RAS_REDIS_PASSWORD", "unused-in-tests")
    get_settings.cache_clear()
    module = importlib.import_module("app.ingest.celery_app")
    monkeypatch.setattr(module, "settings", get_settings())

    def refuse(*args, **kwargs):
        raise AssertionError("a task ran while the recovery gate was closed")
    for name in ("run_sync", "reconcile_pending", "cleanup_revoked", "due_job_ids"):
        monkeypatch.setattr(module, name, refuse)
    return module


# --- the journal file ---------------------------------------------------------

def sample_journal(tmp_path, n: int = 2):
    path = tmp_path / "j" / "revocations.jsonl"
    genesis = journal.genesis(uuid.uuid4(), utcnow(), "new_install")
    chain = [genesis]
    for _ in range(n):
        chain.append(journal.next_record(chain[-1], journal.RECOVERY_BEGIN, {
            "recovery_id": str(uuid.uuid4()), "started_at": utcnow().isoformat(), "db_head_seq": 0}))
        chain.append(journal.next_record(chain[-1], journal.RECOVERY_END, {
            "recovery_id": chain[-1].body["recovery_id"], "finished_at": utcnow().isoformat(), "replayed": 0,
            "tombstones_purged": 0, "objects_removed": 0}))
    journal.create(path, chain)
    return path, chain


def test_journal_verifies_its_chain_and_refuses_every_tampering(tmp_path):
    path, chain = sample_journal(tmp_path)
    state = journal.read(path)
    assert [r.seq for r in state.records] == [1, 2, 3, 4, 5] and state.torn is None
    assert state.head.hash == chain[-1].hash and state.open_recovery is None
    data = path.read_bytes()
    lines = data.split(b"\n")[:-1]

    def corrupt(new: bytes) -> str:
        with pytest.raises(journal.JournalError) as caught:
            journal.parse(new)
        return caught.value.code

    swapped = b"\n".join([lines[0], lines[2], lines[1]] + lines[3:]) + b"\n"
    dropped_middle = b"\n".join(lines[:2] + lines[3:]) + b"\n"
    edited = data.replace(b'"replayed":0', b'"replayed":1', 1)
    spaced = data.replace(b'{"body":', b'{ "body":', 1)
    extra_field = data.replace(b'"kind":"genesis"', b'"kind":"genesis","title":"x"', 1)
    not_genesis_first = b"\n".join(lines[1:]) + b"\n"
    for bad in (b"", swapped, dropped_middle, edited, spaced, extra_field, not_genesis_first, b"not json\n"):
        assert corrupt(bad) == "journal_corrupt"
    # A record may carry only IDs, codes, times and retention bases: a title or any other field is refused.
    body = {name: None for name in journal.REVOCATION_FIELDS} | {"retentions": [], "title": "Paper"}
    with pytest.raises(journal.JournalError, match="journal_corrupt"):
        journal.next_record(chain[-1], journal.REVOCATION, body)
    # An unfinished append (no newline) is a torn tail, not a record.
    torn = journal.parse(data + lines[1][:30])
    assert torn.torn == lines[1][:30] and len(torn.records) == 5
    # Nested recovery_begin is refused.
    nested = journal.next_record(chain[1], journal.RECOVERY_BEGIN, chain[1].body | {"recovery_id": str(uuid.uuid4())})
    with pytest.raises(journal.JournalError, match="journal_corrupt"):
        journal.parse(b"".join(r.line.encode() + b"\n" for r in chain[:2]) + nested.line.encode() + b"\n")
    # Never replaced; append checks the file still holds exactly what was verified.
    with pytest.raises(journal.JournalError, match="journal_exists"):
        journal.create(path, chain[:1])
    with open(path, "ab") as handle:
        handle.write(b"x")
    with pytest.raises(journal.JournalError, match="journal_changed"):
        journal.append(path, state, [journal.next_record(state.head, journal.RECOVERY_BEGIN, chain[1].body)])


def test_cached_read_follows_appends_and_replacements(tmp_path):
    path, chain = sample_journal(tmp_path, n=1)
    first = journal.read_cached(path)
    assert first.head.seq == 3 and journal.read_cached(path) is first  # unchanged file: no re-read
    appended = journal.append(path, first, [journal.next_record(first.head, journal.RECOVERY_BEGIN, chain[1].body)])
    assert journal.read_cached(path).head.seq == appended.head.seq == 4
    assert journal.read_cached(path).open_recovery == chain[1].body["recovery_id"]
    path.write_bytes(path.read_bytes()[:-5])
    assert journal.read_cached(path).torn is not None
    path.unlink()
    with pytest.raises(journal.JournalError, match="journal_missing"):
        journal.read_cached(path)


# --- registration -------------------------------------------------------------

def test_registration_paths_never_take_a_database_with_content_for_a_new_install(app_env, empty_db_url, tmp_path,
                                                                                  monkeypatch):
    upgrade(empty_db_url)
    path = tmp_path / "journal" / "revocations.jsonl"
    monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", str(path))
    app_env(empty_db_url)
    engine = app_db.get_engine()
    assert gate(engine) == "journal_missing"
    assert TestClient(app).get("/readyz").status_code == 503
    assert TestClient(app).get("/auth/csrf").status_code == 503
    with engine.begin() as conn:  # content: a restored database is never "new"
        conn.execute(text("INSERT INTO library_items (visibility, publication_state, title)"
                          " VALUES ('common', 'listed', 'x')"))
    with pytest.raises(RecoveryError, match="database_not_new"):
        recovery.init_new(engine, get_settings(), utcnow())
    assert recovery_cli.main(["init", "--if-new"]) == 0  # Compose's call: skips, changes nothing
    assert not path.exists() and gate(engine) == "journal_missing"
    assert recovery_cli.main(["register-existing"]) == 2  # needs the explicit confirmation
    assert recovery_cli.main(["register-existing", "--confirm-db-is-current"]) == 0
    assert recovery.evaluate_integrity(get_settings(),engine).open
    assert gate(engine) == "auth_safety_pending" and recovery_cli.main(["status"]) == 3
    assert TestClient(app).get("/readyz").status_code == 503
    with pytest.raises(RecoveryError, match="already_registered"):
        recovery.register_existing(engine, get_settings(), utcnow())
    with pytest.raises(RecoveryError, match="already_registered"):
        recovery.init_new(engine, get_settings(), utcnow())
    (genesis,) = records()
    assert genesis.body["mode"] == "existing_db"
    # Another journal file never belongs to this database.
    path.unlink()
    journal.create(path, [journal.genesis(uuid.uuid4(), utcnow(), "new_install")])
    assert gate(engine) == "journal_mismatch" and recovery_cli.main(["status"]) == 3
    with pytest.raises(RecoveryError, match="journal_mismatch"):
        do_recover(engine, None, utcnow(), confirm_db_current=True)
    monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", "")
    get_settings.cache_clear()
    assert gate(engine) == "journal_not_configured"


def test_db_refuses_tombstones_without_journal_record_and_edits_to_the_mirror(api):
    admin = api.bootstrap_admin()
    with pytest.raises(IntegrityError, match="needs its revocation journal record"), api.engine.begin() as conn:
        conn.execute(text("""INSERT INTO deletion_tombstones(target_kind, item_id, reason_code, prior_publication_state,
            rights_epoch, recorded_at, recorded_by_user_id, purge_due_at)
            VALUES ('common_copy', gen_random_uuid(), 'other', 'listed', 1, now(), :a, now() + interval '1 day')"""),
            {"a": admin})
    for statement in ("UPDATE revocation_journal_records SET result='x'", "DELETE FROM revocation_journal_records",
                      "UPDATE recovery_registrations SET mode='adopted'", "DELETE FROM recovery_registrations",
                      "TRUNCATE revocation_journal_records"):
        with pytest.raises(IntegrityError, match="append-only"), api.engine.begin() as conn:
            conn.execute(text(statement))
    assert gate(api.engine) == "open"


# --- revocation writes the journal before t_rev --------------------------------

def test_revocation_is_journaled_and_mirrored_before_it_reports_success(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    assert revoke(w.admin, w.copy).status_code == 200
    # Real runtime bootstrap/invite/login now add auth records to this chain.
    genesis, approval, rec = records()[0], records('review_closure')[-1], records('revocation')[-1]
    assert approval.kind == "review_closure" and approval.body["status"] == "approved"
    assert rec.kind == "revocation" and rec.body["item_id"] == w.copy and rec.prev == approval.hash
    assert set(rec.body) == set(journal.REVOCATION_FIELDS) | {"retentions"}
    assert rec.body["storage_key"] == common_key(w.api, w.copy)
    assert mirror(w.api.engine) == [(r.seq, r.kind, r.hash, r.line) for r in records()]
    tomb = w.api.scalar("SELECT id FROM deletion_tombstones WHERE item_id=:i", i=uuid.UUID(w.copy))
    assert rec.body["id"] == str(tomb)
    assert gate(w.api.engine) == "open"
    assert w.b.get(f"/library/items/{w.copy}").json() == MISSING


def test_journal_write_failure_still_closes_the_copy_and_keeps_the_gate_shut_until_repaired(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    before=records()
    real = journal.append

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(journal, "append", broken)
    response = revoke(w.admin, w.copy)
    monkeypatch.setattr(journal, "append", real)
    assert (response.status_code, response.json()) == (503, {"error": "revocation_journal_unavailable"})
    assert state(w.api, w.copy) == "revoked"  # t_rev happened: fail closed
    assert records()==before
    assert gate(w.api.engine) == "journal_behind_db"
    assert_recovering(w.browsers, [("GET", "/library/items"), ("GET", f"/library/items/{w.ids_a['item_id']}")])
    with pytest.raises(RecoveryError, match="journal_behind_db"):
        do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    report = do_recover(w.api.setup_engine, w.storage, w.api.clock.now, confirm_db_current=True)
    assert report.journal_repaired == 1 and report.gate == "ready"
    assert [(r.seq, r.kind, r.hash, r.line) for r in records()] == mirror(w.api.engine)
    assert records("revocation")[0].body["item_id"] == w.copy
    assert w.b.get(f"/library/items/{w.copy}").json() == MISSING
    assert not key_present(w.storage, common_key(w.api, w.copy))  # recover also ran cleanup


def fsync_fails(monkeypatch):
    """Every os.fsync raises (the line itself is written in full); returns a function that restores it."""
    import os

    real = os.fsync

    def refuse(fd):
        raise OSError("fsync: I/O error")
    monkeypatch.setattr(os, "fsync", refuse)
    return lambda: monkeypatch.setattr(os, "fsync", real)


def unconfirmed(engine) -> list[int]:
    with engine.connect() as conn:
        return list(conn.scalars(text("SELECT seq FROM revocation_journal_records WHERE NOT journal_confirmed"
                                      " ORDER BY seq")).all())


def test_complete_line_whose_fsync_fails_keeps_the_gate_shut_until_recover_confirms_it(upload_api, monkeypatch):
    """PR #26 review: the whole line is in the file and matches the DB copy, but fsync raised, so it was
    never confirmed durable. The revocation still commits (fail closed), the API answers an error and the
    gate must stay closed; only recover, after its own successful fsync, confirms the record."""
    w = listed(upload_api, monkeypatch, copies=2)  # the second copy stays pending
    initial_count=len(records())
    restore = fsync_fails(monkeypatch)
    response = revoke(w.admin, w.copy)
    restore()
    assert (response.status_code, response.json()) == (503, {"error": "revocation_journal_unavailable"})
    assert state(w.api, w.copy) == "revoked"
    # The complete line is in the file and identical to the DB copy: the case the gate used to open on.
    (rec,) = records("revocation")
    assert rec.body["item_id"] == w.copy
    assert [(r.seq, r.kind, r.hash, r.line) for r in records()] == mirror(w.api.engine)
    assert unconfirmed(w.api.engine) == [rec.seq]
    assert gate(w.api.engine) == "journal_unconfirmed"
    assert TestClient(app).get("/readyz").status_code == 503
    assert_recovering(w.browsers, [("GET", "/library/items"), ("GET", f"/library/items/{w.ids_a['item_id']}"),
                                   ("GET", f"/files/{w.ids_a['file_id']}/original")])
    # No further revocation is journaled behind the unconfirmed line.
    with Session(w.api.engine) as db, pytest.raises(ApiError) as caught:
        publication.revoke(db, admin_principal(w.api), uuid.UUID(w.copies[1]), RevocationReason.OTHER,
                           w.api.clock.now, timedelta(hours=24))
    assert (caught.value.status_code, caught.value.code) == (503, "service_recovering")
    assert state(w.api, w.copies[1]) == "pending" and len(records()) == initial_count+1
    # recover cannot confirm while fsync still fails: the gate stays closed.
    restore = fsync_fails(monkeypatch)
    with pytest.raises(RecoveryError, match="journal_sync_failed"):
        do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    restore()
    assert unconfirmed(w.api.engine) == [rec.seq] and gate(w.api.engine) == "journal_unconfirmed"
    # Once the file syncs, recover confirms the record (no --confirm-db-is-current: the line is in the file).
    report = do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    assert report.journal_confirmed == 1 and report.journal_repaired == 0 and report.gate == "ready"
    assert unconfirmed(w.api.engine) == []
    assert w.b.get(f"/library/items/{w.copy}").json() == MISSING


def test_only_confirmation_may_change_a_journal_record(api):
    """The mirror stays append-only except journal_confirmed false -> true, every other column unchanged."""
    statements = ("UPDATE revocation_journal_records SET journal_confirmed=false",
                  "UPDATE revocation_journal_records SET journal_confirmed=true, result='x'",
                  "UPDATE revocation_journal_records SET result='x'")
    for statement in statements:
        with pytest.raises(IntegrityError, match="append-only"), api.engine.begin() as conn:
            conn.execute(text(statement))
    with api.engine.begin() as conn:  # an unconfirmed row may be confirmed, and nothing else
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE revocation_journal_records SET journal_confirmed=false"))
    assert gate(api.engine) == "journal_unconfirmed"
    with api.engine.begin() as conn:
        conn.execute(text("UPDATE revocation_journal_records SET journal_confirmed=true"))
    assert gate(api.engine) == "open"


def test_torn_journal_line_is_moved_aside_and_the_record_restored_from_the_database(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    real = journal.append

    def torn(path, state, new):
        with open(path, "ab") as handle:
            handle.write(new[0].line.encode()[:40])
        raise OSError("interrupted")
    monkeypatch.setattr(journal, "append", torn)
    response = revoke(w.admin, w.copy)
    monkeypatch.setattr(journal, "append", real)
    assert response.json() == {"error": "revocation_journal_unavailable"} and state(w.api, w.copy) == "revoked"
    assert gate(w.api.engine) == "journal_torn"
    with pytest.raises(RecoveryError, match="journal_behind_db"):  # the tail is moved aside, the DB is still ahead
        do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    assert list(journal_file().parent.glob("revocations.jsonl.torn-*"))
    assert gate(w.api.engine) == "journal_behind_db"
    assert do_recover(w.api.setup_engine, w.storage, w.api.clock.now, confirm_db_current=True).gate == "open"
    assert [(r.seq, r.hash) for r in records()] == [(m[0], m[2]) for m in mirror(w.api.engine)]


def test_torn_tail_of_an_append_that_never_committed_is_dropped_without_confirmation(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    with open(journal_file(), "ab") as handle:  # a process died while appending, before its DB commit
        handle.write(b'{"body":{"id":"')
    assert gate(w.api.engine) == "journal_torn"
    assert_recovering(w.browsers, [("GET", "/library/items")])
    report = do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    assert report.torn_tail_dropped and report.gate == "ready"
    assert state(w.api, w.copy) == "listed" and w.copy in listed_ids(w.b)


def failing_commit(monkeypatch, *, lands: bool):
    """The first Session.commit after the journal line raises; ``lands``: it committed anyway (lost reply)."""
    real_execute, real_commit = Session.execute, Session.commit
    armed = []

    def execute(self, statement, *args, **kwargs):
        out = real_execute(self, statement, *args, **kwargs)
        if 'ras_auth_common_revocation_mirror' in str(statement): armed.append(True)
        return out

    def commit(self):
        if armed:
            armed.clear()
            if lands:
                real_commit(self)
            else:
                self.rollback()
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        return real_commit(self)
    monkeypatch.setattr(Session, "execute", execute)
    monkeypatch.setattr(Session, "commit", commit)
    return lambda: (monkeypatch.setattr(Session, "execute", real_execute),
                    monkeypatch.setattr(Session, "commit", real_commit))


@pytest.mark.parametrize("lands", [False, True], ids=["commit_failed", "commit_outcome_unknown"])
def test_runtime_commit_exception_never_replays_until_operator_recovery(upload_api, monkeypatch, lands):
    w = listed(upload_api, monkeypatch)
    restore = failing_commit(monkeypatch, lands=lands)
    response = revoke(w.admin, w.copy)
    restore()
    assert (response.status_code,response.json()) == (503,{'error':'revocation_outcome_uncertain'})
    assert state(w.api,w.copy) == ('revoked' if lands else 'listed')
    assert gate(w.api.engine) == ('open' if lands else 'db_behind_journal')
    do_recover(w.api.setup_engine,w.storage,w.api.clock.now)
    assert state(w.api, w.copy) == "revoked" and gate(w.api.engine) == "open"
    (rec,) = records("revocation")
    assert next(row[2] for row in mirror(w.api.engine) if row[0]==rec.seq) == rec.hash
    via = w.api.scalar("SELECT applied_via FROM revocation_journal_records WHERE seq=:s", s=rec.seq)
    assert via == ("revoke" if lands else "replay")
    assert w.b.get(f"/library/items/{w.copy}").json() == MISSING


def test_runtime_commit_exception_never_invokes_replay_and_gate_stays_shut(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    restore = failing_commit(monkeypatch, lands=False)
    real_replay = recovery.replay_record

    called=[]
    def replay_fails(*args, **kwargs):
        called.append(True)
        raise OperationalError("SELECT", {}, Exception("database gone"))
    monkeypatch.setattr(recovery, "replay_record", replay_fails)
    response = revoke(w.admin, w.copy)
    restore()
    monkeypatch.setattr(recovery, "replay_record", real_replay)
    assert (response.status_code, response.json()) == (503, {"error": "revocation_outcome_uncertain"})
    assert not called
    # The DB rolled back, but the journal line is a decision: nothing is served until it is applied.
    assert state(w.api, w.copy) == "listed" and gate(w.api.engine) == "db_behind_journal"
    assert_recovering(w.browsers, [("GET", "/library/items"), ("GET", f"/library/items/{w.copy}")])
    report = do_recover(w.api.setup_engine, w.storage, w.api.clock.now)  # journal -> DB needs no confirmation
    assert report.replayed["revoked"] == 1 and report.gate == "ready"
    assert state(w.api, w.copy) == "revoked" and w.copy not in listed_ids(w.b)


def test_when_neither_journal_nor_database_take_the_revocation_nothing_changes(upload_api, monkeypatch):
    """The one window where a failed revoke leaves the copy served: the Admin gets 503, no state moved."""
    w = listed(upload_api, monkeypatch)
    before=records()
    real = journal.append

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(journal, "append", broken)
    restore = failing_commit(monkeypatch, lands=False)
    response = revoke(w.admin, w.copy)
    restore()
    monkeypatch.setattr(journal, "append", real)
    assert (response.status_code, response.json()) == (503, {"error": "revocation_outcome_uncertain"})
    assert state(w.api, w.copy) == "listed" and records()==before
    assert len(mirror(w.api.engine)) == len(before) and gate(w.api.engine) == "open"
    assert revoke(w.admin, w.copy).status_code == 200  # a retry revokes it


def test_revocation_while_the_journal_is_ahead_writes_nothing(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    state_before = journal.read(journal_file())
    journal.append(journal_file(), state_before, [journal.next_record(state_before.head, journal.RECOVERY_BEGIN, {
        "recovery_id": str(uuid.uuid4()), "started_at": utcnow().isoformat(), "db_head_seq": 1})])
    assert gate(w.api.engine) == "db_behind_journal"
    assert revoke(w.admin, w.copy).json() == {"error": "service_recovering"}  # the middleware
    with Session(w.api.engine) as db, pytest.raises(ApiError) as caught:  # and revoke itself, behind it
        publication.revoke(db, admin_principal(w.api), uuid.UUID(w.copy), RevocationReason.OTHER, w.api.clock.now,
                           timedelta(hours=24))
    assert (caught.value.status_code, caught.value.code) == (503, "service_recovering")
    assert state(w.api, w.copy) == "listed" and len(mirror(w.api.engine)) == 2 and len(records()) == 3


def test_concurrent_revocations_get_consecutive_journal_records(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch, copies=3)
    for copy in w.copies[1:]:
        assert publish(w.admin, copy).status_code == 409  # same checksum as the first: stays pending
    barrier = Barrier(4)
    outcomes: list = []

    def run(copy):
        browser=w.api.admin_browser();browser.csrf()
        barrier.wait()
        response=revoke(browser,copy,'other')
        outcomes.append((copy,200) if response.status_code==200 else
                        (copy,response.status_code,response.json()['error']))
    threads = [Thread(target=run, args=(c,)) for c in (w.copies[0], w.copies[1], w.copies[2], w.copies[2])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o for o in outcomes if o[0] != w.copies[2]) == sorted([(w.copies[0], 200), (w.copies[1], 200)])
    assert sorted(o[1:] for o in outcomes if o[0] == w.copies[2]) == [(200,), (409, "common_copy_revoked")]
    revocations = records("revocation")
    assert [r.seq for r in records()] == list(range(1,len(records())+1))
    assert len(revocations)==3
    assert sorted(r.body["item_id"] for r in revocations) == sorted(w.copies)
    assert mirror(w.api.engine) == [(r.seq, r.kind, r.hash, r.line) for r in records()]
    assert gate(w.api.engine) == "open"


def test_gate_check_waits_briefly_for_the_journal_lock_then_counts_as_closed(api, monkeypatch):
    monkeypatch.setenv("RAS_RECOVERY__GATE_LOCK_TIMEOUT_MS", "100")
    get_settings.cache_clear()
    holder = create_engine(api.engine.url, hide_parameters=True)
    try:
        with holder.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": recovery.JOURNAL_LOCK})
            assert gate(api.engine) == "journal_lock_busy"
            assert TestClient(app).get("/readyz").status_code == 503
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": recovery.JOURNAL_LOCK})
            conn.commit()
        assert gate(api.engine) == "open"
    finally:
        holder.dispose()


# --- a lost, older or corrupt journal -----------------------------------------

def test_lost_older_or_corrupt_journal_closes_the_gate_and_is_rebuilt_only_on_confirmation(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    assert revoke(w.admin, w.copy).status_code == 200
    path = journal_file()
    good = path.read_bytes()
    routes = [("GET", "/library/items"), ("GET", f"/files/{w.ids_a['file_id']}/original")]

    path.unlink()  # lost
    assert gate(w.api.engine) == "journal_missing"
    assert_recovering(w.browsers, routes)
    with pytest.raises(RecoveryError, match="journal_missing"):
        do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    report = do_recover(w.api.setup_engine, w.storage, w.api.clock.now, confirm_db_current=True)
    assert report.journal_rebuilt == 3 and report.gate == "ready"  # genesis, approval, revocation
    assert path.read_bytes().startswith(good)  # the same lines, then this recovery's begin/end

    older = path.read_bytes().split(b"\n")
    path.write_bytes(b"\n".join(older[:1]) + b"\n")  # truncated to the genesis (an older copy of the file)
    assert gate(w.api.engine) == "journal_behind_db"
    assert_recovering(w.browsers, routes)
    with pytest.raises(RecoveryError, match="journal_behind_db"):
        do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    assert do_recover(w.api.setup_engine, w.storage, w.api.clock.now, confirm_db_current=True).journal_repaired == 4

    current = path.read_bytes()
    lines = current.split(b"\n")
    lines[2] = lines[2].replace(b'"rights_withdrawn"', b'"rights_expired"').replace(  # the revocation record
        b'"rights_recorded_in_error"', b'"rights_expired"')
    path.write_bytes(b"\n".join(lines))  # edited in place
    assert gate(w.api.engine) == "journal_corrupt"
    assert_recovering(w.browsers, routes)
    for confirm in (False, True):  # never rewritten automatically
        with pytest.raises(RecoveryError, match="journal_corrupt"):
            do_recover(w.api.setup_engine, w.storage, w.api.clock.now, confirm_db_current=confirm)
    path.write_bytes(current)
    assert gate(w.api.engine) == "open"
    assert w.b.get(f"/library/items/{w.copy}").json() == MISSING


# --- restore ------------------------------------------------------------------

def content_requests(w) -> list[tuple]:
    return [("GET", "/library/items?limit=100"), ("GET", f"/library/items/{w.ok}"),
            ("GET", f"/files/{w.ok_file}/original"), ("GET", f"/library/items/{w.ok}/chunks"),
            ("GET", f"/search/chunks/{w.ok_chunks[0]}"), ("GET", f"/admin/common-copies/{w.ok}"),
            ("GET", f"/collections/{w.collection}/items"),
            ("POST", "/search", {"query": "Sharedcanary", "mode": "keyword"}),
            ("POST", "/rag/ask", {"question": QUESTION, "mode": "keyword", "item_id": w.ok}),
            ("POST", "/rag/ask", {"question": QUESTION, "mode": "keyword", "scope": "all"})]


def test_restore_from_before_a_revocation_serves_and_processes_nothing_until_recover(upload_api, monkeypatch,
                                                                                     admin_engine, app_env):
    """Indexed common copy with an original + B's private copy of the same bytes; backup; revoke and
    clean up; restore the database and objects, keeping the journal."""
    w = build(upload_api, monkeypatch)
    api, storage = w.api, w.storage
    ok_key = common_key(api, w.ok)
    job = api.scalar("SELECT id FROM ingestion_jobs WHERE item_id=:i", i=uuid.UUID(w.ok))
    assert w.b.get(f"/files/{w.ok_file}/original").status_code == 200  # the copy's original is open before
    b_same_before = item_rows(api.engine, w.b_same["item_id"])
    a_main_before = item_rows(api.engine, w.a_main["item_id"])
    saved = objects(storage)
    b_key = api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(w.b_same["file_id"]))
    with backup(api, admin_engine) as snap:
        assert revoke(w.admin, w.ok).status_code == 200
        assert runtime_cleanup.cleanup_revoked(api.engine, storage) == {"deleted": 2}
        assert not key_present(storage, ok_key)

        app_env(snap.url)
        put_back(storage, saved)
        with snap.engine.connect() as conn:  # the restored database would serve the copy...
            assert tuple(conn.execute(text("SELECT publication_state, content_status FROM library_items WHERE id=:i"),
                                      {"i": uuid.UUID(w.ok)}).one()) == ("listed", "indexed")
        assert gate(snap.engine) == "db_behind_journal"
        # ...but nothing is served to anyone, and not to a guest either.
        assert_recovering(w.browsers, content_requests(w))
        assert TestClient(app).get("/readyz").status_code == 503
        assert TestClient(app).get(f"/library/items/{w.ok}").json() == RECOVERING[1]
        # Workers process nothing and send nothing.
        sent: list[str] = []
        monkeypatch.setattr("app.ingest.indexer.embed", lambda value, settings: sent.append(value))
        assert process_job(job, w.settings, storage) == "recovery_gate_closed"
        assert process_job(uuid.UUID(w.b_same["job_id"]), w.settings, storage) == "recovery_gate_closed"
        tasks = celery_tasks(monkeypatch)
        assert tasks.discover() == 0
        for task in (tasks.sync_arxiv, tasks.reconcile_citation_links, tasks.cleanup_revoked_copies):
            assert task() == {"skipped": "recovery_gate_closed"}
        assert sent == []

        report = do_recover(snap.engine, storage, api.clock.now)
        assert report.replayed == {"recovery_begin": 1, "revoked": 1, "recovery_end": 1}
        assert report.cleanup == {"deleted": 2} and report.gate == "ready"  # this copy and the one revoked earlier
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT ras_common_copy_derived_left(:i)"), {"i": uuid.UUID(w.ok)}) == []
            assert conn.scalar(text("SELECT count(*) FROM chunks WHERE item_id=:i"), {"i": uuid.UUID(w.ok)}) == 0
            assert conn.scalar(text("""SELECT count(*) FROM embedding_calls c JOIN source_files f
                ON f.id=c.source_file_id WHERE f.item_id=:i AND c.vector IS NOT NULL"""), {"i": uuid.UUID(w.ok)}) == 0
        assert not key_present(storage, ok_key)
        for who, browser in w.browsers.items():
            for path in (f"/library/items/{w.ok}", f"/files/{w.ok_file}/original", f"/library/items/{w.ok}/chunks",
                         f"/search/chunks/{w.ok_chunks[0]}"):
                assert browser.get(path).json() == MISSING, (who, path)
            assert w.ok not in hit_items(search(browser)), who
            assert ask(browser, item_id=w.ok).json() == MISSING, who
        # B's private copy of the same bytes, and A's own file, are exactly as before.
        assert item_rows(snap.engine, w.b_same["item_id"]) == b_same_before
        assert item_rows(snap.engine, w.a_main["item_id"]) == a_main_before
        assert hashlib.sha256(storage.open(b_key).read()).hexdigest() == hashlib.sha256(saved[b_key]).hexdigest()
        assert w.b.get(f"/files/{w.b_same['file_id']}/original").status_code == 200
        assert w.b_same["item_id"] in hit_items(search(w.b))
        sent_evidence = answering(monkeypatch)
        answered = ask(w.b)
        assert answered.status_code == 200, answered.text
        assert w.ok not in {row["item_id"] for payload in sent_evidence for row in payload}
        assert w.ok not in answered.text


def test_restore_replays_revocations_of_copies_the_snapshot_never_had(upload_api, monkeypatch, admin_engine,
                                                                      app_env):
    w = listed(upload_api, monkeypatch)
    a_private_before = item_rows(w.api.engine, w.ids_a["item_id"])
    b_private_before = item_rows(w.api.engine, w.ids_b["item_id"])
    with backup(w.api, admin_engine) as snap:
        later = new_copy(w.api, w.a, w.admin, w.ids_a)  # created after the backup
        later_key = common_key(w.api, later)
        assert revoke(w.admin, w.copy).status_code == 200
        assert revoke(w.admin, later, retain=[{"field": "title", "policy_source": "Test policy",
                                              "scope": "Audit only", "reason": "Kept for the test"}],
                      confirm_retention_basis=True).status_code == 200
        saved = objects(w.storage)  # an object backup taken after the database backup

        app_env(snap.url)
        put_back(w.storage, saved)
        assert gate(snap.engine) == "db_behind_journal"
        report = do_recover(snap.engine, w.storage, w.api.clock.now)
        # The later copy's approval closed a request the snapshot never had: only its mirror row is added.
        assert report.replayed == {"recovery_begin": 1, "revoked": 1, "recorded_item_absent": 1, "recovery_end": 1,
                                   "recorded_request_absent": 1}
        assert report.gate == "ready"
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM library_items WHERE id=:i"), {"i": uuid.UUID(later)}) == 0
            tomb = conn.execute(text("SELECT purged_at IS NOT NULL, object_outcome FROM deletion_tombstones"
                                     " WHERE item_id=:i"), {"i": uuid.UUID(later)}).one()
            assert tuple(tomb) == (True, "deleted")
            assert conn.scalar(text("SELECT count(*) FROM revocation_metadata_retentions WHERE item_id=:i"),
                               {"i": uuid.UUID(later)}) == 0  # nothing to keep: no FK to a missing row
        assert not key_present(w.storage, later_key) and not key_present(w.storage, common_key(w.api, w.copy))
        assert item_rows(snap.engine, w.ids_a["item_id"]) == a_private_before
        assert item_rows(snap.engine, w.ids_b["item_id"]) == b_private_before
        assert w.a.get(f"/files/{w.ids_a['file_id']}/original").content == A_BODY
        assert w.b.get(f"/library/items/{w.copy}").json() == MISSING


def test_adopts_a_database_older_than_its_registration(upload_api, monkeypatch, admin_engine, app_env):
    """A database restored from before `init` (here: freshly migrated, never registered) replays everything."""
    w = listed(upload_api, monkeypatch)
    assert revoke(w.admin, w.copy).status_code == 200
    fresh = create_empty_database(admin_engine)
    try:
        upgrade(fresh)
        app_env(fresh)
        old = app_db.get_engine()
        assert gate(old) == "not_registered"
        assert_recovering(w.browsers, [("GET", "/library/items")])
        report = do_recover(old, w.storage, w.api.clock.now)
        assert report.adopted and report.gate == "ready"
        assert report.replayed == {"genesis": 1, "recorded_item_absent": 1, "recovery_begin": 1, "recovery_end": 1,
                                   "recorded_request_absent": 1}
        assert mirror(old) == [(r.seq, r.kind, r.hash, r.line) for r in records()]
    finally:
        app_db.get_engine().dispose()
        app_db.get_engine.cache_clear()
        drop_database(admin_engine, fresh)


def test_cleanup_failure_during_recovery_keeps_the_gate_shut_until_a_rerun_succeeds(upload_api, monkeypatch,
                                                                                    admin_engine, app_env):
    w = listed(upload_api, monkeypatch)
    key = common_key(w.api, w.copy)
    with backup(w.api, admin_engine) as snap:
        assert revoke(w.admin, w.copy).status_code == 200
        app_env(snap.url)
        real_delete = w.storage.delete

        def unavailable(key):
            raise OperationalError("DELETE", {}, Exception("storage down"))
        monkeypatch.setattr(w.storage, "delete", unavailable)
        with pytest.raises(RecoveryError, match="cleanup_incomplete"):
            do_recover(snap.engine, w.storage, w.api.clock.now)
        assert gate(snap.engine) == "recovery_in_progress"
        assert state_in(snap.engine, w.copy) == "revoked"  # replayed, but not yet cleaned up
        assert_recovering(w.browsers, [("GET", "/library/items"), ("GET", f"/files/{w.ids_a['file_id']}/original")])
        monkeypatch.setattr(w.storage, "delete", real_delete)
        report = do_recover(snap.engine, w.storage, w.api.clock.now)
        assert report.gate == "ready" and not key_present(w.storage, key)
        kinds = [r.kind for r in records()]
        assert kinds == ["genesis", "review_closure", "revocation", "recovery_begin",
                         "recovery_end"]  # the rerun closed the same run
        # And again: idempotent, a fresh begin/end pair, nothing replayed.
        assert do_recover(snap.engine, w.storage, w.api.clock.now).replayed == {"recovery_begin": 1,
                                                                                 "recovery_end": 1}
        assert recovery.apply_record(Session(snap.engine), records("revocation")[0], utcnow()) == ("present", 0)


def state_in(engine, item: str) -> str:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT publication_state FROM library_items WHERE id=:i"), {"i": uuid.UUID(item)})


def test_a_record_naming_a_private_item_is_refused_and_touches_nothing(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    assert revoke(w.admin, w.copy).status_code == 200
    (real,) = records("revocation")
    forged = real.body | {"id": str(uuid.uuid4()), "item_id": w.ids_b["item_id"], "version_id": None,
                          "source_file_id": None, "storage_key": None}
    current = journal.read(journal_file())
    journal.append(journal_file(), current, [journal.next_record(current.head, journal.REVOCATION, forged)])
    before = item_rows(w.api.engine, w.ids_b["item_id"])
    assert gate(w.api.engine) == "db_behind_journal"
    with pytest.raises(RecoveryError, match="record_names_another_item"):
        do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    assert item_rows(w.api.engine, w.ids_b["item_id"]) == before
    assert gate(w.api.engine) == "db_behind_journal"  # the refused record and the open recovery are not applied
    assert_recovering(w.browsers, [("GET", f"/library/items/{w.ids_b['item_id']}")])


def test_only_one_recovery_runs_at_a_time(api):
    holder = create_engine(api.engine.url, hide_parameters=True)
    try:
        with holder.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": recovery.RECOVER_LOCK})
            with pytest.raises(RecoveryError, match="recovery_already_running"):
                do_recover(api.engine, None, utcnow())
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": recovery.RECOVER_LOCK})
            conn.commit()
    finally:
        holder.dispose()
    assert gate(api.engine) == "open"


def test_indexer_checks_the_gate_right_before_each_embedding_call(upload_api, monkeypatch):
    from tests.test_ingest_worker import upload
    from tests.test_private_index import fake_provider, settings_with_fake_key

    api, storage = upload_api
    api.create_user("a@example.org")
    ids = upload(api.user_browser("a@example.org"), b"## Methods\nFirst body.\n\n## Results\nSecond body.\n")
    job = uuid.UUID(ids["job_id"])
    assert process_job(job, storage=storage) == "awaiting_index"
    calls = fake_provider(monkeypatch)
    real = recovery.gate_open
    # Open when the job starts; closes (as after a journal failure elsewhere) before the first call leaves.
    monkeypatch.setattr(recovery, "gate_open", lambda settings, engine, where: where != "indexer"
                        and real(settings, engine, where))
    assert process_job(job, settings_with_fake_key(), storage) == "recovery_gate_closed"
    assert calls == []
    row = api.scalar("SELECT state || ':' || last_error_code FROM ingestion_jobs WHERE id=:j", j=job)
    assert row == "retryable_failed:recovery_gate_closed"
    assert api.scalar("SELECT count(*) FROM embedding_calls WHERE status IN ('reserved', 'completed')") == 0
    assert api.scalar("SELECT count(*) FROM index_entries") == 0


def test_objects_restored_from_an_older_object_backup_are_removed_by_recover(upload_api, monkeypatch, admin_engine,
                                                                            app_env):
    """The DB is current (its tombstone is purged) but the object store comes from before the revocation."""
    w = listed(upload_api, monkeypatch)
    key = common_key(w.api, w.copy)
    older_objects = objects(w.storage)
    assert revoke(w.admin, w.copy).status_code == 200
    assert runtime_cleanup.cleanup_revoked(w.api.engine, w.storage) == {"deleted": 1}
    put_back(w.storage, older_objects)
    assert key_present(w.storage, key)
    # The database says revoked and purged: nothing serves the object, and the gate cannot see it...
    assert gate(w.api.engine) == "open"
    assert w.a.get(f"/files/{common_file(w.api, w.copy)}/original").json() == MISSING
    # ...so the runbook runs `recover` after any restore, and its sweep removes it.
    report = do_recover(w.api.setup_engine, w.storage, w.api.clock.now)
    assert report.objects_removed == 1 and report.gate == "ready" and not key_present(w.storage, key)
    assert w.a.get(f"/files/{w.ids_a['file_id']}/original").content == A_BODY
