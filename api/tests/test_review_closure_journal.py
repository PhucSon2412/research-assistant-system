"""M6c-2b1: a closed review grant stays closed after a restore (PostgreSQL + MinIO, journal files in tmp).

A backup taken while a review request is open is restored after the owner withdrew it or an
Admin rejected or approved it. The database side of a backup is ``CREATE DATABASE ... TEMPLATE``
(as in ``test_recovery``), the object side an M6c-2a artifact restored with
``app.backup.restore_objects`` (the bucket then holds exactly the artifact's objects), and the
journal a ``copy_journal`` copy of the current one, kept apart. Crashes are simulated by making the
exact step raise. No provider is called (nothing here indexes).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from threading import Barrier, Thread
from types import SimpleNamespace

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import backup as bk
from app import db as app_db
from app import journal, recovery
from app.api.errors import ApiError
from app.api.routes import reviews
from app.clock import utcnow
from app.config import get_settings
from app.main import app
from app.models.review import closed_status_of
from app.recovery import RecoveryError
from tests.conftest import alembic_config, drop_database, upgrade
from tests.test_backup import make_artifact
from tests.test_publication import MISSING
from tests.test_recovery import (RECOVERING, assert_recovering, backup, do_recover, fsync_fails, gate, item_rows,
                                 journal_file, mirror, records, unconfirmed)
from tests.test_review_approval import A_SHA, ADMIN_EVIDENCE, ADMIN_SOURCE, approve, object_keys
from tests.test_review_requests import (A_BODY, B_BODY, DECLARED_RIGHTS, DECLARED_SOURCE, close_journaled,
                                        original_url, submit, world, write)
from tests.test_ingest_worker import upload
from tests.test_uploads import upload_api  # noqa: F401  (fixture)

NOTE = "Synthetic rejection note RNOTE-41 (must never reach the journal)"
CODES = {"withdraw": "withdrawn_by_requester", "reject": "rights_not_established", "approve": "approved_by_admin"}
DECISION = {"withdraw": "withdrawn", "reject": "rejected", "approve": "approved"}
# Nothing of the request but IDs, codes and times may enter the journal.
NOT_IN_JOURNAL = (DECLARED_SOURCE, DECLARED_RIGHTS, NOTE, "a@example.org", "b@example.org", A_SHA, ADMIN_SOURCE,
                  ADMIN_EVIDENCE, "Reviewable", "synthetic.pdf", "Bản chung đã kiểm")


def review_world(upload_api, monkeypatch):
    """A and B each have one open request for their own file; the Admin has opened A's (saved URLs)."""
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch)
    rid = submit(a, ids_a).json()["request"]["id"]
    rid_b = submit(b, ids_b).json()["request"]["id"]
    w = SimpleNamespace(api=api, storage=storage, a=a, b=b, admin=admin, ids_a=ids_a, ids_b=ids_b, rid=rid,
                        rid_b=rid_b, browsers={"a": a, "b": b, "admin": admin})
    w.detail_url, w.file_url = f"/admin/review-requests/{rid}", original_url(rid, ids_a["file_id"])
    assert admin.get(w.file_url).content == A_BODY
    assert admin.get(w.detail_url).json()["request"]["review_available"] is True
    return w


def close(w, how: str):
    if how == "withdraw":
        return write(w.a, "POST", f"/review-requests/{w.rid}/withdraw")
    if how == "reject":
        return write(w.admin, "POST", f"/admin/review-requests/{w.rid}/reject",
                     {"reason_code": CODES["reject"], "note": NOTE})
    return approve(w.admin, w.api, w.rid)


def status_in(engine, rid) -> tuple:
    with engine.connect() as conn:
        return tuple(conn.execute(text("SELECT status, close_reason_code, closed_by_user_id IS NULL, rejection_note"
                                       " FROM review_requests WHERE id=:r"), {"r": uuid.UUID(rid)}).one())


def closures() -> list[journal.Record]:
    return records("review_closure")


def assert_grant_closed_for_admin(w, browser=None) -> None:
    """The saved review URLs and the list give the Admin nothing of the private file any more."""
    admin = browser or w.admin
    assert admin.get(w.file_url).json() == MISSING
    detail = admin.get(w.detail_url).json()["request"]
    assert detail["review_available"] is False
    assert not {"item", "file", "version", "requester_email", "declaration", "original_url"} & set(detail)
    assert w.rid not in {r["id"] for r in admin.get("/admin/review-requests?status=open").json()["requests"]}
    closed = {r["id"]: r for r in admin.get("/admin/review-requests?status=closed").json()["requests"]}
    assert not {"item", "file", "requester_email", "declaration", "original_url"} & set(closed[w.rid])


# --- the record --------------------------------------------------------------

def test_each_close_journals_ids_decision_code_and_time_only(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, ids_a, ids_b = world(api, storage, monkeypatch)
    rids = {}
    for how in ("withdraw", "reject", "approve"):
        rids[how] = submit(a, ids_a).json()["request"]["id"]
        w = SimpleNamespace(a=a, admin=admin, api=api, rid=rids[how])
        assert close(w, how).status_code == 200
    body = {r.body["request_id"]: r for r in closures()}
    assert set(body) == set(rids.values())
    for how, rid in rids.items():
        record = body[rid]
        assert set(record.body) == set(journal.CLOSURE_FIELDS)
        assert (record.body["status"], record.body["reason_code"]) == (DECISION[how], CODES[how])
        assert (record.body["item_id"], record.body["version_id"], record.body["source_file_id"]) == (
            ids_a["item_id"], ids_a["version_id"], ids_a["file_id"])
        via = api.scalar("SELECT applied_via || ':' || result || ':' || journal_confirmed::text"
                         " FROM revocation_journal_records WHERE review_request_id=:r", r=uuid.UUID(rid))
        assert via == "close:closed:true"
    assert mirror(api.engine) == [(r.seq, r.kind, r.hash, r.line) for r in records()]
    text_of_journal = journal_file().read_text(encoding="ascii")
    assert all(secret not in text_of_journal for secret in NOT_IN_JOURNAL)
    assert gate(api.engine) == "open"
    # The reader refuses anything else in a closure record, and a code that names another decision.
    head = journal.read(journal_file()).head
    good = body[rids["reject"]].body
    for bad in (good | {"note": NOTE}, good | {"status": "withdrawn"}, good | {"reason_code": "approved_by_admin"},
                good | {"status": "closed_by_recovery"}, good | {"request_id": None}):
        with pytest.raises(journal.JournalError, match="journal_corrupt"):
            journal.next_record(head, journal.REVIEW_CLOSURE, bad)
    assert [closed_status_of(c) for c in CODES.values()] == ["withdrawn", "rejected", "approved"]


def test_db_refuses_a_close_without_the_record_naming_that_target_and_code(upload_api, monkeypatch):
    api, _ = upload_api
    api.clock.advance(hours=1)  # SQL must use the fixture clock, independently of DB wall time.
    w = review_world(upload_api, monkeypatch)
    closed_at = w.api.clock.now
    withdraw = text("""UPDATE review_requests SET status='withdrawn', closed_at=:closed_at,
        closed_by_user_id=requester_user_id, close_reason_code='withdrawn_by_requester' WHERE id=:r""")
    with pytest.raises(IntegrityError, match="needs its revocation journal record"), w.api.engine.begin() as conn:
        conn.execute(withdraw, {"r": uuid.UUID(w.rid), "closed_at": closed_at})
    # A record naming B's file, or A's file with another decision, does not cover A's withdraw.
    close_journaled(w.api.engine, w.rid_b)
    head = journal.read(journal_file()).head
    a_target = {"request_id": w.rid, "item_id": w.ids_a["item_id"], "version_id": w.ids_a["version_id"],
                "source_file_id": w.ids_a["file_id"], "closed_at": closed_at.isoformat()}
    other_file = a_target | {"item_id": w.ids_b["item_id"], "version_id": w.ids_b["version_id"],
                             "source_file_id": w.ids_b["file_id"]}
    for body in (other_file | {"status": "withdrawn", "reason_code": "withdrawn_by_requester"},
                 a_target | {"status": "rejected", "reason_code": "other"}):
        record = journal.next_record(head, journal.REVIEW_CLOSURE, body)
        with pytest.raises(IntegrityError, match="needs its revocation journal record"), w.api.engine.begin() as conn:
            conn.execute(withdraw, {"r": uuid.UUID(w.rid), "closed_at": closed_at})
            conn.execute(text("""INSERT INTO revocation_journal_records(seq, kind, record_hash, line,
                review_request_id, applied_via, result, journal_confirmed)
                VALUES(:s, 'review_closure', :h, :l, :r, 'close', 'closed', true)"""),
                         {"s": record.seq, "h": record.hash, "l": record.line, "r": uuid.UUID(w.rid)})
    # closed_by_recovery carries the original code and time, never an actor or a note.
    for sets in ("close_reason_code='other', closed_by_user_id=requester_user_id",
                 "close_reason_code='other', rejection_note='x'", "close_reason_code='made_up'"):
        with pytest.raises(IntegrityError), w.api.engine.begin() as conn:
            conn.execute(text(f"""UPDATE review_requests SET status='closed_by_recovery', closed_at=:closed_at, {sets}
                WHERE id=:r"""), {"r": uuid.UUID(w.rid), "closed_at": closed_at})
    assert status_in(w.api.engine, w.rid)[0] == "open" and gate(w.api.engine) == "open"
    assert w.admin.get(w.file_url).content == A_BODY


# --- restore ------------------------------------------------------------------

@pytest.mark.parametrize("how", ["withdraw", "reject", "approve"])
def test_restore_of_a_backup_with_the_request_open_does_not_give_the_grant_back(upload_api, monkeypatch, admin_engine,
                                                                                 app_env, tmp_path, how):
    w = review_world(upload_api, monkeypatch)
    a_before = item_rows(w.api.engine, w.ids_a["item_id"])
    b_before = item_rows(w.api.engine, w.ids_b["item_id"])
    with backup(w.api, admin_engine) as snap:
        artifact = make_artifact(tmp_path / "backups", w.storage, snap.engine)
        keys_at_backup = object_keys(w.storage)

        closed = close(w, how)
        assert closed.status_code == 200, closed.text
        assert w.admin.get(w.file_url).json() == MISSING  # live: the grant ended at the commit
        if how == "approve":
            copy = closed.json()["request"]["pending_publication_id"]
            assert len(object_keys(w.storage) - keys_at_backup) == 1  # the common copy's object
        (record,) = closures()
        assert record.body["status"] == DECISION[how]

        # Restore: the old database and objects, the current journal (a copy kept apart).
        manifest = bk.verify_artifact(artifact)
        copied = tmp_path / "journal-copy"
        bk.copy_journal(journal_file(), copied, utcnow(), apart_from=[tmp_path / "backups"])
        bk.check_journal_for_backup(journal.read(copied / bk.JOURNAL_FILE), manifest)
        monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", str(copied / bk.JOURNAL_FILE))
        app_env(snap.url)
        assert bk.restore_objects(w.storage, artifact, manifest)["deleted"] == (1 if how == "approve" else 0)
        assert object_keys(w.storage) == keys_at_backup
        assert status_in(snap.engine, w.rid)[0] == "open"  # the snapshot would give the grant back...
        recovery.hold(snap.engine, get_settings(), utcnow())
        assert gate(snap.engine) == "db_behind_journal"
        # ...but before recover nobody gets anything, the saved review URLs included.
        assert_recovering(w.browsers, [("GET", w.file_url), ("GET", w.detail_url), ("GET", "/admin/review-requests"),
                                       ("GET", f"/review-requests/{w.rid}"),
                                       ("GET", f"/files/{w.ids_a['file_id']}/original")])
        assert TestClient(app).get("/readyz").status_code == 503

        report = do_recover(snap.engine, w.storage, w.api.clock.now)
        assert report.replayed == {"recovery_begin": 1, "closed": 1, "recovery_end": 1} and report.gate == "open"
        assert status_in(snap.engine, w.rid) == ("closed_by_recovery", CODES[how], True, None)
        assert_grant_closed_for_admin(w)
        # The owner sees the original decision's code; the request is not an approval.
        mine = w.a.get(f"/review-requests/{w.rid}").json()["request"]
        assert (mine["status"], mine["close_reason_code"]) == ("closed_by_recovery", CODES[how])
        assert mine["pending_note"] == reviews.RECOVERED_NOTE and mine["publication"] is False
        assert "pending_publication_id" not in w.admin.get(w.detail_url).json()["request"]
        # B's request was still open when the source was lost: it stays reviewable; B never sees A's.
        assert w.admin.get(original_url(w.rid_b, w.ids_b["file_id"])).content == B_BODY
        assert w.b.get(f"/review-requests/{w.rid}").json() == MISSING
        assert w.b.get("/review-requests").json()["total"] == 1
        # The owners keep their items and files exactly; no common copy exists that the snapshot lacked.
        assert item_rows(snap.engine, w.ids_a["item_id"]) == a_before
        assert item_rows(snap.engine, w.ids_b["item_id"]) == b_before
        assert w.a.get(f"/files/{w.ids_a['file_id']}/original").content == A_BODY
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM library_items WHERE visibility='common'")) == 0
            assert conn.scalar(text("SELECT count(*) FROM access_rights")) == 0
        assert w.admin.get("/admin/pending-publications").json()["total"] == 0
        if how == "approve":
            assert w.admin.get(f"/admin/common-copies/{copy}").json() == MISSING
            assert w.b.get(f"/library/items/{copy}").json() == MISSING
        # Replay again: nothing changes.
        assert do_recover(snap.engine, w.storage, w.api.clock.now).replayed == {"recovery_begin": 1,
                                                                                 "recovery_end": 1}
        with Session(snap.engine) as db:
            assert recovery.apply_record(db, record, utcnow()) == ("present", 0)
        assert status_in(snap.engine, w.rid)[0] == "closed_by_recovery"
        # A common copy comes back only through the normal flow: a new request and a new approval.
        again = submit(w.a, w.ids_a).json()["request"]["id"]
        assert w.admin.get(original_url(again, w.ids_a["file_id"])).content == A_BODY
        approved = approve(w.admin, w.api, again)
        assert approved.status_code == 200 and closures()[-1].body["request_id"] == again
        assert w.admin.get(original_url(again, w.ids_a["file_id"])).json() == MISSING
        assert gate(snap.engine) == "open"


def clone(admin_engine, url):
    """Another restore of the same snapshot: a new database made from it."""
    copy = url.set(database=f"ras_test_{uuid.uuid4().hex[:12]}")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{copy.database}" TEMPLATE "{url.database}"'))
    return copy


def test_restoring_the_same_backup_twice_ends_the_same_way(upload_api, monkeypatch, admin_engine, app_env):
    w = review_world(upload_api, monkeypatch)
    with backup(w.api, admin_engine) as snap:
        assert close(w, "withdraw").status_code == 200
        assert close(SimpleNamespace(**{**vars(w), "rid": w.rid_b}), "reject").status_code == 200
        snap.engine.dispose()
        ends = []
        for _ in range(2):
            url = clone(admin_engine, snap.url)
            try:
                app_env(url)
                engine = app_db.get_engine()
                recovery.hold(engine, get_settings(), utcnow())
                assert gate(engine) == "db_behind_journal"
                report = do_recover(engine, w.storage, w.api.clock.now)
                assert report.replayed["closed"] == 2 and report.gate == "open"
                ends.append((status_in(engine, w.rid), status_in(engine, w.rid_b)))
                assert_grant_closed_for_admin(w)
                assert w.admin.get(original_url(w.rid_b, w.ids_b["file_id"])).json() == MISSING
            finally:
                app_db.get_engine().dispose()
                app_db.get_engine.cache_clear()
                drop_database(admin_engine, url)
    assert ends == [(("closed_by_recovery", CODES["withdraw"], True, None),
                     ("closed_by_recovery", CODES["reject"], True, None))] * 2


def test_a_record_naming_another_target_is_refused_and_the_gate_stays_shut(upload_api, monkeypatch):
    w = review_world(upload_api, monkeypatch)
    state = journal.read(journal_file())
    forged = {"request_id": w.rid, "item_id": w.ids_b["item_id"], "version_id": w.ids_b["version_id"],
              "source_file_id": w.ids_b["file_id"], "status": "withdrawn", "reason_code": "withdrawn_by_requester",
              "closed_at": utcnow().isoformat()}
    journal.append(journal_file(), state, [journal.next_record(state.head, journal.REVIEW_CLOSURE, forged)])
    assert gate(w.api.engine) == "db_behind_journal"
    with pytest.raises(RecoveryError, match="record_names_another_request"):
        do_recover(w.api.engine, w.storage, w.api.clock.now)
    assert status_in(w.api.engine, w.rid)[0] == "open" and gate(w.api.engine) == "db_behind_journal"
    assert_recovering(w.browsers, [("GET", w.file_url)])


# --- a record names the request it is filed under (PR #28 review, revision 0029) -----------------

MIRROR_INSERT = text("""INSERT INTO revocation_journal_records(seq, kind, record_hash, line, review_request_id,
    applied_via, result, journal_confirmed) VALUES(:s, 'review_closure', :h, :l, :r, 'close', 'closed', true)""")


def closure_of(w, request_id, *, status="withdrawn", code="withdrawn_by_requester") -> dict:
    """A well-formed closure body for A's file whose ``request_id`` may name any request."""
    return {"request_id": str(request_id), "item_id": w.ids_a["item_id"], "version_id": w.ids_a["version_id"],
            "source_file_id": w.ids_a["file_id"], "status": status, "reason_code": code,
            "closed_at": utcnow().isoformat()}


def test_db_refuses_at_insert_a_mirror_row_whose_line_names_another_or_no_request(upload_api, monkeypatch):
    # Keep fake time well ahead of PostgreSQL's wall clock: the journal fence,
    # not an unrelated closed_after CHECK, must be the reason the SQL is refused.
    upload_api[0].clock.advance(hours=1)
    w = review_world(upload_api, monkeypatch)
    head = journal.read(journal_file()).head
    absent = str(uuid.uuid4())
    for filed_under, named in ((w.rid, w.rid_b), (w.rid, absent), (absent, w.rid)):
        record = journal.next_record(head, journal.REVIEW_CLOSURE, closure_of(w, named))
        with w.api.engine.connect() as conn:
            # Refused by the INSERT itself (not at commit), so a writer stops before appending the line.
            with pytest.raises(IntegrityError, match="closure_request_matches_body"):
                conn.execute(MIRROR_INSERT, {"s": record.seq, "h": record.hash, "l": record.line,
                                             "r": uuid.UUID(filed_under)})
            conn.rollback()
    # A request the database does not hold is a valid record when it is filed under that same ID
    # (a restored snapshot may lack a request the journal names).
    record = journal.next_record(head, journal.REVIEW_CLOSURE, closure_of(w, absent))
    with w.api.engine.connect() as conn:
        conn.execute(MIRROR_INSERT, {"s": record.seq, "h": record.hash, "l": record.line, "r": uuid.UUID(absent)})
        conn.rollback()
    # The closing trigger compares body.request_id with the request on its own too: with the CHECK
    # dropped (inside the transaction, rolled back), a row filed under A's request whose line names B's
    # does not let A's request close.
    record = journal.next_record(head, journal.REVIEW_CLOSURE, closure_of(w, w.rid_b))
    with pytest.raises(IntegrityError, match="needs its revocation journal record"), w.api.engine.begin() as conn:
        conn.execute(text("ALTER TABLE revocation_journal_records"
                          " DROP CONSTRAINT ck_revocation_journal_records_closure_request_matches_body"))
        conn.execute(MIRROR_INSERT, {"s": record.seq, "h": record.hash, "l": record.line, "r": uuid.UUID(w.rid)})
        conn.execute(text("""UPDATE review_requests SET status='withdrawn', closed_at=:closed,
            closed_by_user_id=requester_user_id, close_reason_code='withdrawn_by_requester' WHERE id=:r"""),
                     {"r": uuid.UUID(w.rid), "closed": w.api.clock.now})
    assert w.api.scalar("SELECT count(*) FROM pg_constraint"
                        " WHERE conname='ck_revocation_journal_records_closure_request_matches_body'") == 1
    assert status_in(w.api.engine, w.rid)[0] == "open" and closures() == [] and gate(w.api.engine) == "open"
    assert w.admin.get(w.file_url).content == A_BODY


@pytest.mark.parametrize("shape", ["missing", "json_null"])
def test_db_refuses_at_insert_a_mirror_row_whose_line_has_no_request_id(upload_api, monkeypatch, shape):
    """PR #28 review, second round: ``=`` gave NULL for a missing or null body.request_id, and a CHECK lets
    NULL through; the comparison must be TRUE for the row to be accepted."""
    upload_api[0].clock.advance(hours=1)
    w = review_world(upload_api, monkeypatch)
    head = journal.read(journal_file()).head
    value = json.loads(journal.next_record(head, journal.REVIEW_CLOSURE, closure_of(w, w.rid)).line)
    if shape == "missing":
        del value["body"]["request_id"]
    else:
        value["body"]["request_id"] = None
    line = journal.canonical(value)
    params = {"s": head.seq + 1, "h": "e" * 64, "l": line, "r": uuid.UUID(w.rid)}
    with w.api.engine.connect() as conn:
        with pytest.raises(IntegrityError, match="closure_request_matches_body"):
            conn.execute(MIRROR_INSERT, params)  # refused by the INSERT itself
        conn.rollback()
    with pytest.raises(IntegrityError, match="closure_request_matches_body"), w.api.engine.begin() as conn:
        conn.execute(text("""UPDATE review_requests SET status='withdrawn', closed_at=:closed,
            closed_by_user_id=requester_user_id, close_reason_code='withdrawn_by_requester' WHERE id=:r"""),
                     {"r": uuid.UUID(w.rid), "closed": w.api.clock.now})
        conn.execute(MIRROR_INSERT, params)
    # The other layers refuse it too: the reader never accepts such a line.
    with pytest.raises(journal.JournalError, match="journal_corrupt"):
        journal.parse_line(line.encode("ascii"), head.seq + 1, head.hash)
    assert status_in(w.api.engine, w.rid)[0] == "open" and closures() == [] and gate(w.api.engine) == "open"
    assert w.admin.get(w.file_url).content == A_BODY


@pytest.mark.parametrize("named", ["other_request", "no_request"])
@pytest.mark.parametrize("how", ["withdraw", "reject", "approve"])
def test_a_close_whose_record_names_another_request_writes_nothing_and_the_grant_stays_open(
        upload_api, monkeypatch, how, named):
    w = review_world(upload_api, monkeypatch)
    real = recovery.closure_body
    wrong = w.rid_b if named == "other_request" else str(uuid.uuid4())
    monkeypatch.setattr(recovery, "closure_body", lambda rr: real(rr) | {"request_id": wrong})
    journal_before, mirror_before, keys = journal_file().read_bytes(), mirror(w.api.engine), object_keys(w.storage)
    response = close(w, how)
    expected = "approval_unavailable" if how == "approve" else "review_closure_unavailable"
    assert (response.status_code, response.json()) == (503, {"error": expected})
    # The DB refused the mirror row before the append: the journal file and the mirror are unchanged.
    assert journal_file().read_bytes() == journal_before and mirror(w.api.engine) == mirror_before
    assert status_in(w.api.engine, w.rid)[0] == "open" and status_in(w.api.engine, w.rid_b)[0] == "open"
    assert w.api.scalar("SELECT count(*) FROM library_items WHERE visibility='common'") == 0
    assert object_keys(w.storage) == keys  # approve's copy, unreferenced, was removed
    assert gate(w.api.engine) == "open"
    assert w.admin.get(w.file_url).content == A_BODY  # never reported closed, so still open
    monkeypatch.setattr(recovery, "closure_body", real)
    assert close(w, how).status_code == 200
    (record,) = closures()
    assert record.body["request_id"] == w.rid
    assert_grant_closed_for_admin(w)
    assert w.admin.get(original_url(w.rid_b, w.ids_b["file_id"])).content == B_BODY


def test_a_request_journaled_after_the_backup_stays_absent_and_its_record_is_valid(upload_api, monkeypatch,
                                                                                   admin_engine, app_env):
    w = review_world(upload_api, monkeypatch)
    with backup(w.api, admin_engine) as snap:
        assert close(w, "withdraw").status_code == 200
        later = submit(w.a, w.ids_a).json()["request"]["id"]  # sent and withdrawn after the backup
        assert write(w.a, "POST", f"/review-requests/{later}/withdraw").status_code == 200
        app_env(snap.url)
        recovery.hold(snap.engine, get_settings(), utcnow())
        report = do_recover(snap.engine, w.storage, w.api.clock.now)
        assert report.replayed == {"recovery_begin": 1, "closed": 1, "recorded_request_absent": 1,
                                   "recovery_end": 1} and report.gate == "open"
        with snap.engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM review_requests WHERE id=:r"), {"r": uuid.UUID(later)}) == 0
            filed = conn.execute(text("SELECT review_request_id::text, line::jsonb #>> '{body,request_id}', result"
                                      " FROM revocation_journal_records WHERE review_request_id=:r"),
                                 {"r": uuid.UUID(later)}).one()
        assert tuple(filed) == (later, later, "recorded_request_absent")
        assert w.admin.get(original_url(later, w.ids_a["file_id"])).json() == MISSING
        assert status_in(snap.engine, w.rid)[0] == "closed_by_recovery"
        assert_grant_closed_for_admin(w)


# --- failure windows of the close itself ----------------------------------------

def failing_close_commit(monkeypatch, *, lands: bool):
    """The first commit after the closure record raises; ``lands``: it committed anyway (lost reply)."""
    real_record, real_commit = recovery.record_review_closure, Session.commit
    armed = []

    def record(*args, **kwargs):
        out = real_record(*args, **kwargs)
        armed.append(True)
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
    monkeypatch.setattr(recovery, "record_review_closure", record)
    monkeypatch.setattr(Session, "commit", commit)
    return lambda: (monkeypatch.setattr(recovery, "record_review_closure", real_record),
                    monkeypatch.setattr(Session, "commit", real_commit))


def broken_append(monkeypatch, effect=None):
    real = journal.append

    def broken(path, state, new):
        if effect:
            effect(path, new)
        raise OSError("disk full")
    monkeypatch.setattr(journal, "append", broken)
    return lambda: monkeypatch.setattr(journal, "append", real)


@pytest.mark.parametrize("how", ["withdraw", "reject", "approve"])
def test_journal_write_failure_still_ends_the_grant_and_shuts_the_gate_until_repaired(upload_api, monkeypatch, how):
    w = review_world(upload_api, monkeypatch)
    restore = broken_append(monkeypatch)
    response = close(w, how)
    restore()
    assert (response.status_code, response.json()) == (503, {"error": "review_journal_unavailable"})
    assert status_in(w.api.engine, w.rid)[0] == DECISION[how]  # committed: the grant ended (fail closed)
    assert closures() == [] and gate(w.api.engine) == "journal_behind_db"
    assert_recovering(w.browsers, [("GET", w.file_url), ("GET", w.detail_url)])
    with pytest.raises(RecoveryError, match="journal_behind_db"):
        do_recover(w.api.engine, w.storage, w.api.clock.now)
    assert do_recover(w.api.engine, w.storage, w.api.clock.now, confirm_db_current=True).gate == "open"
    assert [r.body["request_id"] for r in closures()] == [w.rid]
    assert_grant_closed_for_admin(w)


def test_complete_closure_line_whose_fsync_fails_keeps_the_gate_shut_until_recover_confirms_it(upload_api,
                                                                                               monkeypatch):
    w = review_world(upload_api, monkeypatch)
    restore = fsync_fails(monkeypatch)
    response = close(w, "withdraw")
    restore()
    assert (response.status_code, response.json()) == (503, {"error": "review_journal_unavailable"})
    (record,) = closures()
    assert [(r.seq, r.kind, r.hash, r.line) for r in records()] == mirror(w.api.engine)
    assert unconfirmed(w.api.engine) == [record.seq] and gate(w.api.engine) == "journal_unconfirmed"
    # No later close is journaled behind an unconfirmed line.
    with pytest.raises(ApiError) as caught:
        close_journaled(w.api.engine, w.rid_b)
    assert (caught.value.status_code, caught.value.code) == (503, "service_recovering")
    assert status_in(w.api.engine, w.rid_b)[0] == "open"
    report = do_recover(w.api.engine, w.storage, w.api.clock.now)
    assert report.journal_confirmed == 1 and report.gate == "open"
    assert_grant_closed_for_admin(w)


def test_torn_closure_line_shuts_the_gate_and_needs_the_database_confirmed(upload_api, monkeypatch):
    w = review_world(upload_api, monkeypatch)

    def torn(path, new):
        with open(path, "ab") as handle:
            handle.write(new[0].line.encode()[:40])
    restore = broken_append(monkeypatch, torn)
    assert close(w, "withdraw").json() == {"error": "review_journal_unavailable"}
    restore()
    assert status_in(w.api.engine, w.rid)[0] == "withdrawn" and gate(w.api.engine) == "journal_torn"
    with pytest.raises(RecoveryError, match="journal_behind_db"):
        do_recover(w.api.engine, w.storage, w.api.clock.now)
    assert do_recover(w.api.engine, w.storage, w.api.clock.now, confirm_db_current=True).gate == "open"
    assert_grant_closed_for_admin(w)


@pytest.mark.parametrize("lands", [False, True], ids=["commit_failed", "commit_outcome_unknown"])
@pytest.mark.parametrize("how", ["withdraw", "reject"])
def test_journaled_close_whose_commit_fails_is_replayed_at_once(upload_api, monkeypatch, how, lands):
    w = review_world(upload_api, monkeypatch)
    restore = failing_close_commit(monkeypatch, lands=lands)
    response = close(w, how)
    restore()
    assert response.status_code == 200, response.text
    # A lost reply leaves the real decision; a failed commit is completed by the replay (no actor, no note).
    expected = DECISION[how] if lands else "closed_by_recovery"
    assert response.json()["request"]["status"] == expected == status_in(w.api.engine, w.rid)[0]
    assert gate(w.api.engine) == "open" and len(closures()) == 1
    via = w.api.scalar("SELECT applied_via FROM revocation_journal_records WHERE review_request_id=:r",
                       r=uuid.UUID(w.rid))
    assert via == ("close" if lands else "replay")
    assert_grant_closed_for_admin(w)


@pytest.mark.parametrize("lands", [False, True], ids=["commit_failed", "commit_outcome_unknown"])
def test_journaled_approval_whose_commit_fails_never_becomes_an_approval_by_replay(upload_api, monkeypatch, lands):
    w = review_world(upload_api, monkeypatch)
    keys = object_keys(w.storage)
    restore = failing_close_commit(monkeypatch, lands=lands)
    response = approve(w.admin, w.api, w.rid)
    restore()
    copies = w.api.scalar("SELECT count(*) FROM library_items WHERE visibility='common'")
    if lands:  # the approval committed, the reply was lost: it stands, as before M6c-2b1
        assert response.json() == {"error": "approval_outcome_uncertain"}
        assert status_in(w.api.engine, w.rid)[0] == "approved" and copies == 1
    else:  # nothing committed: the durable close ends the grant, with no copy and no approval
        assert response.json() == {"error": "approval_unavailable"}
        assert status_in(w.api.engine, w.rid) == ("closed_by_recovery", "approved_by_admin", True, None)
        assert copies == 0 and w.api.scalar("SELECT count(*) FROM access_rights") == 0
        assert object_keys(w.storage) == keys  # the unreferenced copy was removed
    assert gate(w.api.engine) == "open"
    assert_grant_closed_for_admin(w)


def test_journaled_close_whose_commit_and_replay_fail_keeps_the_gate_shut(upload_api, monkeypatch):
    w = review_world(upload_api, monkeypatch)
    restore = failing_close_commit(monkeypatch, lands=False)
    real_replay = recovery.replay_record

    def replay_fails(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("database gone"))
    monkeypatch.setattr(recovery, "replay_record", replay_fails)
    response = close(w, "withdraw")
    restore()
    monkeypatch.setattr(recovery, "replay_record", real_replay)
    assert (response.status_code, response.json()) == (503, {"error": "review_closure_pending_recovery"})
    # The database still says open, but the journal line is a decision: nothing is served until it is applied.
    assert status_in(w.api.engine, w.rid)[0] == "open" and gate(w.api.engine) == "db_behind_journal"
    assert_recovering(w.browsers, [("GET", w.file_url), ("GET", w.detail_url)])
    report = do_recover(w.api.engine, w.storage, w.api.clock.now)
    assert report.replayed["closed"] == 1 and report.gate == "open"
    assert status_in(w.api.engine, w.rid)[0] == "closed_by_recovery"
    assert_grant_closed_for_admin(w)


def test_when_neither_journal_nor_database_take_the_close_the_grant_is_still_open(upload_api, monkeypatch):
    """The one window where a failed close leaves the grant open: the caller gets 503, nothing moved."""
    w = review_world(upload_api, monkeypatch)
    restore_append = broken_append(monkeypatch)
    restore = failing_close_commit(monkeypatch, lands=False)
    response = close(w, "withdraw")
    restore()
    restore_append()
    assert (response.status_code, response.json()) == (503, {"error": "review_closure_unavailable"})
    assert status_in(w.api.engine, w.rid)[0] == "open" and closures() == [] and gate(w.api.engine) == "open"
    assert w.admin.get(w.file_url).content == A_BODY  # never reported closed, still open
    assert close(w, "withdraw").status_code == 200  # a retry closes it
    assert_grant_closed_for_admin(w)


# --- concurrency ------------------------------------------------------------------

def test_concurrent_closes_journal_one_record_per_request_in_sequence(upload_api, monkeypatch):
    w = review_world(upload_api, monkeypatch)
    barrier = Barrier(3)
    outcomes: dict[str, list] = {"withdraw": [], "reject": [], "reject_b": []}

    def run(name, how, target):
        barrier.wait()
        outcomes[name].append(close(SimpleNamespace(**{**vars(w), "rid": target}), how).status_code)
    threads = [Thread(target=run, args=("withdraw", "withdraw", w.rid)),
               Thread(target=run, args=("reject", "reject", w.rid)),
               Thread(target=run, args=("reject_b", "reject", w.rid_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes["withdraw"] + outcomes["reject"]) == [200, 409] and outcomes["reject_b"] == [200]
    assert sorted(r.body["request_id"] for r in closures()) == sorted([w.rid, w.rid_b])
    assert [r.seq for r in records()] == list(range(1, len(records()) + 1))
    assert mirror(w.api.engine) == [(r.seq, r.kind, r.hash, r.line) for r in records()]
    assert gate(w.api.engine) == "open"


def test_close_during_a_recovery_writes_nothing_and_works_after_it(upload_api, monkeypatch):
    w = review_world(upload_api, monkeypatch)
    recovery.hold(w.api.engine, get_settings(), utcnow())  # a recovery is open (e.g. right after a restore)
    assert close(w, "withdraw").json() == RECOVERING[1]  # the middleware
    with pytest.raises(ApiError) as caught:  # and the close itself, behind it
        close_journaled(w.api.engine, w.rid)
    assert (caught.value.status_code, caught.value.code) == (503, "service_recovering")
    assert status_in(w.api.engine, w.rid)[0] == "open" and closures() == []

    # recover and a close racing it: whichever order, journal, mirror and request agree at the end.
    results: list = []

    def recover_now():
        results.append(("recover", do_recover(w.api.engine, w.storage, w.api.clock.now).gate))

    def close_now():
        try:
            close_journaled(w.api.engine, w.rid)
            results.append(("close", 200))
        except ApiError as exc:
            results.append(("close", exc.code))
    threads = [Thread(target=recover_now), Thread(target=close_now)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ("recover", "open") in results
    if ("close", 200) not in results:
        close_journaled(w.api.engine, w.rid)
    assert status_in(w.api.engine, w.rid)[0] == "withdrawn" and len(closures()) == 1
    assert mirror(w.api.engine) == [(r.seq, r.kind, r.hash, r.line) for r in records()]
    assert gate(w.api.engine) == "open"
    assert_grant_closed_for_admin(w)


def test_a_download_past_its_last_check_finishes_and_a_new_one_after_the_close_is_refused(upload_api, monkeypatch):
    api, storage = upload_api
    a, b, admin, _, _ = world(api, storage, monkeypatch)
    big = b"## Methods\n" + b"Large reviewable body for streaming. " * 12000  # several stream chunks
    ids = upload(a, big)
    rid = submit(a, ids).json()["request"]["id"]
    url = original_url(rid, ids["file_id"])
    real = tempfile.TemporaryFile
    reads = []

    class Spool:
        """The verified spool; the owner's withdraw commits between two chunks of the response."""

        def __init__(self):
            self.inner = real()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def read(self, n=-1):
            reads.append(n)
            if len(reads) == 2:
                done = Thread(target=lambda: write(a, "POST", f"/review-requests/{rid}/withdraw"))
                done.start()
                done.join()
            return self.inner.read(n)
    monkeypatch.setattr(reviews.tempfile, "TemporaryFile", Spool)
    response = admin.get(url)
    monkeypatch.setattr(reviews.tempfile, "TemporaryFile", real)
    assert response.status_code == 200 and response.content == big  # passed its last check before the close
    assert len(reads) > 2 and status_in(api.engine, rid)[0] == "withdrawn"
    assert admin.get(url).json() == MISSING  # every request after the commit is refused
    assert [r.body["request_id"] for r in closures()] == [rid]


# --- upgrade from 0027 ---------------------------------------------------------------

def _settings(path):
    return SimpleNamespace(recovery=SimpleNamespace(journal_path=path, gate_lock_timeout_ms=2000, cleanup_passes=1),
                           retention=SimpleNamespace(cleanup_batch_size=10))


def _legacy_world(conn) -> dict:
    """At 0027: an Admin, owner A with one file, and two open requests for it (different versions)."""
    from app.identity.passwords import hash_password

    admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('admin@x.test',:p,'admin')"
                             " RETURNING id"), {"p": hash_password("m6c2b1-migration-password")})
    owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('a@x.test',:p) RETURNING id"),
                        {"p": hash_password("m6c2b1-migration-password")})
    item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'T')"
                            " RETURNING id"), {"o": owner})
    out = {"admin": admin, "owner": owner, "item": item, "requests": []}
    for n in range(2):
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        checksum = hashlib.sha256(f"legacy {n}".encode()).hexdigest()
        file = conn.scalar(text("""INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,
            checksum_sha256) VALUES(:i,:v,:k,'text/markdown',8,:h) RETURNING id"""),
            {"i": item, "v": version, "k": f"originals/{uuid.uuid4().hex}", "h": checksum})
        out["requests"].append(conn.scalar(text("""INSERT INTO review_requests(item_id,version_id,source_file_id,
            requester_user_id,status,file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,
            declared_rights,submitted_at) VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id"""),
            {"i": item, "v": version, "f": file, "o": owner, "h": checksum}))
    return out


def test_upgrade_from_0027_journals_old_closes_before_the_gate_opens_and_protects_older_backups(
        empty_db_url, admin_engine, tmp_path):
    upgrade(empty_db_url, "0027")
    live = create_engine(empty_db_url)
    path = tmp_path / "journal" / "revocations.jsonl"
    genesis = journal.genesis(uuid.uuid4(), utcnow(), "new_install")
    with live.begin() as conn:
        world0027 = _legacy_world(conn)
        conn.execute(text("INSERT INTO recovery_registrations(id,journal_id,genesis_hash,mode,registered_at)"
                          " VALUES(1,:j,:h,'new_install',now())"), {"j": genesis.body["journal_id"], "h": genesis.hash})
        conn.execute(text("""INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,
            journal_confirmed) VALUES(1,'genesis',:h,:l,'genesis','new_install',true)"""),
                     {"h": genesis.hash, "l": genesis.line})
    journal.create(path, [genesis])
    withdrawn, rejected = world0027["requests"]
    live.dispose()
    old, old2 = clone(admin_engine, live.url), clone(admin_engine, live.url)  # a 0027 backup, both requests open
    older_journal = tmp_path / "older" / "revocations.jsonl"
    older_journal.parent.mkdir()
    older_journal.write_bytes(path.read_bytes())  # a journal copy taken before any import
    try:
        with live.begin() as conn:  # closed under 0027 code: no record anywhere
            conn.execute(text("""UPDATE review_requests SET status='withdrawn', closed_at=now(),
                closed_by_user_id=requester_user_id, close_reason_code='withdrawn_by_requester' WHERE id=:r"""),
                         {"r": withdrawn})
            conn.execute(text("""UPDATE review_requests SET status='rejected', closed_at=now(), closed_by_user_id=:a,
                close_reason_code='other', rejection_note='legacy note' WHERE id=:r"""),
                         {"a": world0027["admin"], "r": rejected})
        upgrade(empty_db_url)
        settings = _settings(path)
        assert recovery.evaluate(settings, live).reason == "review_closures_not_journaled"
        assert recovery.status(live, settings)["unjournaled_review_closures"] == 2
        with pytest.raises(Exception, match="registered with a revocation journal"):
            command.downgrade(alembic_config(empty_db_url), "0027")
        report = recovery.recover(live, None, settings, utcnow())  # no --confirm-db-is-current needed
        assert report.closures_imported == 2 and report.gate == "open"
        imported = {r.body["request_id"]: r.body for r in journal.read(path).records if r.kind == "review_closure"}
        assert {k: (v["status"], v["reason_code"]) for k, v in imported.items()} == {
            str(withdrawn): ("withdrawn", "withdrawn_by_requester"), str(rejected): ("rejected", "other")}
        assert "legacy note" not in path.read_text(encoding="ascii")
        with live.connect() as conn:  # mirrored by the replay, confirmed
            mirrored = conn.execute(text("SELECT result, journal_confirmed FROM revocation_journal_records"
                                         " WHERE kind='review_closure' ORDER BY seq")).all()
            assert mirrored == [("already_closed", True)] * 2

        # The 0027 backup restored with the current journal: held before it is migrated, then replayed.
        old_engine = create_engine(old)
        recovery.hold(old_engine, settings, utcnow())  # works on the restored, not yet migrated schema
        assert recovery.evaluate(settings, old_engine).reason in ("db_behind_journal", "db_not_migrated")
        upgrade(old)
        assert recovery.evaluate(settings, old_engine).reason == "db_behind_journal"
        assert recovery.recover(old_engine, None, settings, utcnow()).gate == "open"
        with old_engine.connect() as conn:
            assert conn.execute(text("SELECT status, close_reason_code FROM review_requests ORDER BY close_reason_code"
                                     )).all() == [("closed_by_recovery", "other"),
                                                  ("closed_by_recovery", "withdrawn_by_requester")]
        old_engine.dispose()

        # The limit of the backfill, stated as a test: the same backup restored with a journal copy taken
        # before the import reopens both grants. That journal never learned of them; nothing can tell.
        older = _settings(older_journal)
        old2_engine = create_engine(old2)
        recovery.hold(old2_engine, older, utcnow())
        upgrade(old2)
        assert recovery.recover(old2_engine, None, older, utcnow()).gate == "open"
        with old2_engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM review_requests WHERE status='open'")) == 2
        old2_engine.dispose()
    finally:
        live.dispose()
        drop_database(admin_engine, old)
        drop_database(admin_engine, old2)


# --- registration ------------------------------------------------------------------

def test_register_existing_journals_closed_requests_too(empty_db_url, tmp_path):
    upgrade(empty_db_url, "0027")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        w0 = _legacy_world(conn)
        conn.execute(text("""UPDATE review_requests SET status='withdrawn', closed_at=now(),
            closed_by_user_id=requester_user_id, close_reason_code='withdrawn_by_requester' WHERE id=:r"""),
                     {"r": w0["requests"][0]})
    upgrade(empty_db_url)
    settings = _settings(tmp_path / "revocations.jsonl")
    _, imported = recovery.register_existing(eng, settings, utcnow())
    assert imported == 1 and recovery.evaluate(settings, eng).reason == "open"
    (record,) = [r for r in journal.read(settings.recovery.journal_path).records if r.kind == "review_closure"]
    assert record.body["request_id"] == str(w0["requests"][0]) and record.body["status"] == "withdrawn"
    eng.dispose()


def test_0028_downgrades_only_a_database_never_registered(empty_db_url):
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), "0027")
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0027"
    eng.dispose()
    upgrade(empty_db_url)


def _closure_row_at_0028(conn, filed_under: uuid.UUID, named: uuid.UUID) -> None:
    genesis = journal.genesis(uuid.uuid4(), utcnow(), "new_install")
    body = {"request_id": str(named), "item_id": str(uuid.uuid4()), "version_id": str(uuid.uuid4()),
            "source_file_id": str(uuid.uuid4()), "status": "withdrawn", "reason_code": "withdrawn_by_requester",
            "closed_at": utcnow().isoformat()}
    record = journal.next_record(genesis, journal.REVIEW_CLOSURE, body)
    conn.execute(text("""INSERT INTO revocation_journal_records(seq, kind, record_hash, line, applied_via, result,
        journal_confirmed) VALUES(1, 'genesis', :h, :l, 'genesis', 'new_install', true)"""),
                 {"h": genesis.hash, "l": genesis.line})
    conn.execute(MIRROR_INSERT, {"s": record.seq, "h": record.hash, "l": record.line, "r": filed_under})


@pytest.mark.parametrize("matching", [True, False], ids=["record_names_its_request", "record_names_another"])
def test_0029_upgrades_matching_closure_rows_and_refuses_a_mismatched_one(empty_db_url, matching):
    upgrade(empty_db_url, "0028")
    eng = create_engine(empty_db_url)
    filed = uuid.uuid4()
    with eng.begin() as conn:  # 0028 accepted either row
        _closure_row_at_0028(conn, filed, filed if matching else uuid.uuid4())
    if matching:
        upgrade(empty_db_url, "0029")
    else:
        with pytest.raises(Exception, match="filed under another request than its line names"):
            upgrade(empty_db_url, "0029")
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == ("0029" if matching else "0028")
    eng.dispose()
