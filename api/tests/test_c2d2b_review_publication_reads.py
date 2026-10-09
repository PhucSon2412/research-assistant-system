"""Nine JSON reads with runtime LOGIN/receipts and a real isolated journal.

Resource rows and corruptions use privileged setup. Business HTTP scopes only
cutover; pending HTTP keeps the real gate. Writer helpers below keep their
production row/journal order; writer HTTP/storage remains outside this task.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety, journal, promotion, publication, recovery
from app.api import review_publication_reads as boundary
from app.api.errors import ApiError
from app.api.routes import reviews, publications
from app.authz import Principal
from app.identity.login import login
from app.main import app
from app.models import DeletionTombstone, LibraryItem, ReviewRequest, UserSession
from app.models.enums import RevocationReason, RevokedMetadataField, RightsAudience
from app.review_closing import commit_close
from tests.resource_fixtures import source, common_copy, runtime_approve, MemoryOriginals as BaseMemoryOriginals, SHA
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import world, issue, accept, PASSWORD, EMAIL
from tests.test_c2d1_final_caller import browser, mutate, close, wait_for
from tests.test_auth_safety_foundation import NOW
from tests.test_review_requests import close_journaled

ROUTES = {
    "notice": (reviews, "notice", "/review-requests/notice"),
    "own_list": (reviews, "list_own", "/review-requests"),
    "own_get": (reviews, "get_own", "/review-requests/{request}"),
    "admin_list": (reviews, "admin_list", "/admin/review-requests"),
    "admin_get": (reviews, "admin_get", "/admin/review-requests/{request}"),
    "pending_list": (publications, "list_pending", "/admin/pending-publications"),
    "pending_get": (publications, "get_pending", "/admin/pending-publications/{item}"),
    "copy_list": (publications, "list_copies", "/admin/common-copies"),
    "copy_get": (publications, "get_copy", "/admin/common-copies/{item}"),
}
ADMIN = list(ROUTES)[3:]
PRIVATE = ("PRIVATE-TITLE", "PRIVATE-DECLARATION", "private-file-marker.md")


class MemoryOriginals(BaseMemoryOriginals):
    def size(self, key):
        if key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return len(self.objects[key])


def request(w, owner, marker="PRIVATE-TITLE"):
    ids = source(w, owner, marker)
    with Session(w.engine, expire_on_commit=False) as db:
        rr = ReviewRequest(item_id=ids.item, version_id=ids.version, source_file_id=ids.file,
            requester_user_id=owner, status="open", file_checksum_sha256=SHA, rights_epoch_at_submit=0,
            notice_version="synthetic", declared_source="PRIVATE-DECLARATION", declared_rights="unverified",
            submitted_at=NOW)
        db.add(rr)
        db.commit()
        ids.request = rr.id
    return ids


@pytest.fixture
def reads(browser):
    b = browser
    b.ids = request(b.w, b.w.uid)
    b.copy = common_copy(b.w, pending=True, indexed=False)
    b.path = lambda key: ROUTES[key][2].format(request=b.ids.request, item=b.copy.item)
    return b


@pytest.mark.parametrize("key", ROUTES)
def test_nine_success_same_transaction_fresh_proofs_no_dml(reads, monkeypatch, key, caplog):
    b = reads
    calls = []
    real = boundary.final_caller
    def checked(db, **kw):
        proof = real(db, **kw)
        calls.append((db.get_transaction(), proof))
        return proof
    monkeypatch.setattr(boundary, "final_caller", checked)
    before = b.w.path.read_bytes()
    def business_counts():
        with b.w.runtime.connect() as conn:
            return tuple(conn.scalar(text(f"SELECT count(*) FROM {table}")) for table in
                         ("audit_events", "ingestion_jobs", "auth_change_receipts"))
    counts = business_counts()
    r = b.client.get(b.path(key))
    assert r.status_code == 200, r.text
    assert len(calls) == 2 and calls[0][0] is calls[1][0]
    assert all(p.session_id == b.w.actor.session_id and p.user_id == b.w.uid for _, p in calls)
    assert b.w.path.read_bytes() == before
    assert business_counts() == counts
    assert not any(marker in caplog.text + b.w.path.read_text() for marker in PRIVATE)
    with Session(b.w.runtime) as db:
        assert db.get(UserSession, b.w.actor.session_id).last_activity_at == NOW
    assert b.w.runtime.pool.checkedout() == 0


@pytest.mark.parametrize("key", ROUTES)
@pytest.mark.parametrize("change", ["revoke", "disable", "generation", "quarantine", "held",
    "receipt_credential", "receipt_status", "wrong_receipt", "head", "latest_credential",
    "latest_status", "hold", "ahead", "unconfirmed"])
def test_auth_after_real_dependency(reads, key, change):
    b = reads
    b.after_dependency = lambda db, ctx: mutate(b, change)
    r = b.client.get(b.path(key))
    expected = 503 if change in ("hold", "ahead", "unconfirmed") else 401
    assert r.status_code == expected, r.text
    assert r.json() == {"error": "service_recovering" if expected == 503 else "session_expired"}
    if expected == 401:
        assert "Max-Age=0" in r.headers["set-cookie"]
    assert not any(marker in r.text for marker in PRIVATE)


@pytest.mark.parametrize("key", ADMIN)
def test_six_current_admin_not_dependency_role(reads, key):
    b = reads
    b.after_dependency = lambda db, ctx: mutate(b, "role")
    r = b.client.get(b.path(key))
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}


@pytest.mark.parametrize("key", ROUTES)
@pytest.mark.parametrize("kind,seconds,expected", [("idle", 1799, 200), ("idle", 1800, 401),
                                                   ("absolute", 28799, 200), ("absolute", 28800, 401)])
def test_final_clock_after_resource_sql(reads, monkeypatch, key, kind, seconds, expected):
    b = reads
    module, name, _ = ROUTES[key]
    real = getattr(module, "_read_" + name)
    calls = []
    def read(*args, **kw):
        value = real(*args, **kw)
        calls.append(1)
        if len(calls) == 3:
            b.at = NOW + timedelta(seconds=seconds)
        return value
    if kind == "absolute":
        def activity(db, ctx):
            with b.w.engine.begin() as conn:
                conn.execute(text("UPDATE sessions SET last_activity_at=:at WHERE id=:s"),
                    {"at": NOW + timedelta(hours=7, minutes=59), "s": ctx.session_id})
        b.after_dependency = activity
    monkeypatch.setattr(module, "_read_" + name, read)
    r = b.client.get(b.path(key))
    assert r.status_code == expected, r.text
    assert len(calls) == 3


@pytest.mark.parametrize("key", ROUTES)
def test_real_pending_nine_http(installation, key):
    with Session(installation.runtime) as db:
        assert auth_safety.cutover(db) == "auth_safety_pending"
    with TestClient(app) as client:
        path = ROUTES[key][2].format(request=uuid.uuid4(), item=uuid.uuid4())
        assert client.get(path).json() == {"error": "service_recovering"}
        assert client.get(path).status_code == 503
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_real_a_b_same_hash_owner_sql_count_page_detail_and_admin_grant(reads):
    b = reads
    a = accept(b.w, issue(b.w))
    other = accept(b.w, issue(b.w, email="other@example.test"))
    ids_a, ids_b = request(b.w, a.id, "A-TITLE"), request(b.w, other.id, "B-TITLE")
    with Session(b.w.runtime) as db:
        token = login(db, email=EMAIL, password=PASSWORD, now=NOW, settings=b.w.settings,
                      clock=b.clock)[1].token
    with TestClient(app, base_url="http://web.test") as owner:
        owner.cookies.set("ras_session", token)
        r = owner.get("/review-requests?limit=1")
        assert r.status_code == 200 and r.json()["total"] == 1
        assert r.json()["requests"][0]["id"] == str(ids_a.request)
        assert "B-TITLE" not in r.text
        assert owner.get(f"/review-requests?item_id={ids_b.item}&limit=1").json()["total"] == 0
        assert owner.get(f"/review-requests/{ids_b.request}").status_code == 404
        assert owner.get(f"/admin/review-requests/{ids_a.request}").status_code == 403
        close_journaled(b.w.engine, ids_a.request)
        rr = owner.get(f"/review-requests/{ids_a.request}").json()["request"]
        assert rr["status"] == "withdrawn" and rr["declaration"]["verified"] is False
        assert "pending_publication_id" not in rr and "rights" not in rr
    for ids in (ids_b,):
        rr = b.client.get(f"/admin/review-requests/{ids.request}").json()["request"]
        assert rr["file"]["id"] == str(ids.file) and rr["version"]["id"] == str(ids.version)
        assert rr["declaration"]["verified"] is False
    data = b.client.get("/admin/review-requests?status=closed&limit=1").json()
    assert data["total"] == 2 and len(data["requests"]) == 1


@pytest.mark.parametrize("bad", ["checksum", "version", "file", "epoch", "owner", "visibility"])
def test_exact_review_grant_invalid_thin_history_keeps_workflow_count(reads, bad):
    b = reads
    sql = {
        "checksum": "UPDATE review_requests SET file_checksum_sha256=repeat('f',64) WHERE id=:r",
        "version": "UPDATE review_requests SET version_id=gen_random_uuid() WHERE id=:r",
        "file": "UPDATE review_requests SET source_file_id=gen_random_uuid() WHERE id=:r",
        "epoch": "UPDATE review_requests SET rights_epoch_at_submit=99 WHERE id=:r",
        "owner": "UPDATE review_requests SET requester_user_id=gen_random_uuid() WHERE id=:r",
        "visibility": "UPDATE library_items SET visibility='common',owner_user_id=NULL,publication_state='listed' WHERE id=:i",
    }[bad]
    with b.w.engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role='replica'"))
        conn.execute(text(sql), {"r": b.ids.request, "i": b.ids.item})
    r = b.client.get(b.path("admin_get"))
    assert r.status_code == 200, r.text
    assert r.json()["request"]["review_available"] is False
    assert not any(marker in r.text for marker in PRIVATE)
    assert not ({"item", "file", "version", "requester_email", "declaration", "original_url"}
                & r.json()["request"].keys())
    r = b.client.get(b.path("admin_list"))
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["requests"][0]["review_available"] is False


def approve_request(b, *, db=None):
    # C2d-5b: approval is runtime only (own Sessions/proofs); a caller Session is never shared.
    return runtime_approve(b.w, b.ids.request, MemoryOriginals(b.ids), title="approved common",
                           source="synthetic permission", evidence="synthetic evidence", search=True, provider=True)


def end_review(b, how, db=None):
    if how == "approve":
        return approve_request(b, db=db)
    if db is None:
        return close_journaled(b.w.engine, b.ids.request, how)
    rr = db.scalar(select(ReviewRequest).where(ReviewRequest.id == b.ids.request).with_for_update())
    rr.closed_at = NOW
    rr.status = "withdrawn" if how == "withdraw" else "rejected"
    rr.closed_by_user_id = b.w.uid
    rr.close_reason_code = "withdrawn_by_requester" if how == "withdraw" else "other"
    return commit_close(db, rr)


@pytest.mark.parametrize("how", ["withdraw", "reject", "approve", "recovery"])
@pytest.mark.parametrize("key", ["admin_list", "admin_get"])
def test_close_between_candidate_and_final_no_private_dto(reads, monkeypatch, how, key):
    b = reads
    real = getattr(reviews, "_read_" + ROUTES[key][1])
    calls = []
    def read(*args, **kw):
        value = real(*args, **kw)
        if not calls:
            calls.append(1)
            if how == "recovery":
                with Session(b.w.engine) as db:
                    rr = db.get(ReviewRequest, b.ids.request)
                    rr.status, rr.closed_at, rr.close_reason_code = "withdrawn", NOW, "withdrawn_by_requester"
                    record = recovery.record_review_closure(db, b.w.path, rr).record
                    db.rollback()
                recovery.replay_record(b.w.engine, record, NOW)
            else:
                end_review(b, how)
        return value
    monkeypatch.setattr(reviews, "_read_" + ROUTES[key][1], read)
    r = b.client.get(b.path(key))
    assert r.status_code == 200, r.text
    assert not any(marker in r.text for marker in PRIVATE)
    if key == "admin_list":
        assert r.json()["total"] == 0 and r.json()["requests"] == []
    else:
        rr = r.json()["request"]
        assert rr["review_available"] is False
        assert ("pending_publication_id" in rr) == (how == "approve")
        if how == "recovery":
            assert rr["status"] == "closed_by_recovery"


def revoke_copy(b, c=None):
    c = c or b.copy
    with Session(b.w.engine) as db:
        db.execute(text("SET LOCAL application_name='c2d2b_writer'"))
        return publication.revoke(db, c.admin, c.item, RevocationReason.RIGHTS_WITHDRAWN, NOW,
                                  timedelta(hours=24))


@pytest.mark.parametrize("how", ["publish", "revoke"])
@pytest.mark.parametrize("key", ["pending_list", "pending_get", "copy_list", "copy_get"])
def test_common_state_before_final_requery_history(reads, monkeypatch, how, key):
    b = reads
    real = getattr(publications, "_read_" + ROUTES[key][1])
    calls = []
    def read(*args, **kw):
        value = real(*args, **kw)
        if not calls:
            calls.append(1)
            if how == "publish":
                storage = MemoryOriginals(b.copy)
                from tests.resource_fixtures import runtime_publish
                runtime_publish(b.w, b.copy.item, storage)
            else:
                revoke_copy(b)
        return value
    monkeypatch.setattr(publications, "_read_" + ROUTES[key][1], read)
    r = b.client.get(b.path(key))
    assert r.status_code == (404 if key == "pending_get" else 200), r.text
    if key.endswith("list"):
        assert r.json()["total"] == 0 and r.json()["items"] == []
    elif key == "copy_get":
        assert r.json()["item"]["publication_state"] == ("listed" if how == "publish" else "revoked")


@pytest.mark.parametrize("download,search,provider,indexed,pending", [
    (False, True, False, True, False), (True, True, True, True, False),
    (True, False, False, False, False), (False, True, True, False, False),
    (True, True, True, False, True)])
def test_common_flags_separate_real_gates(reads, download, search, provider, indexed, pending):
    b = reads
    c = common_copy(b.w, download=download, search=search, provider=provider, indexed=indexed, pending=pending)
    r = b.client.get(f"/admin/common-copies/{c.item}")
    assert r.status_code == 200, r.text
    row = r.json()["item"]
    assert row["fulltext_available"] == bool(indexed and search and not pending)
    assert row["index"]["search_open"] == bool(indexed and search and not pending)
    assert row["index"]["rag_open"] == bool(indexed and search and provider and not pending)
    assert (row["original_download"] == "open") == (download and not pending)
    assert "storage_key" not in r.text and "COMMON-TEXT" not in r.text


def writer_wait(w, approval=False):
    deadline = time.monotonic() + 10
    with w.engine.connect() as observer:
        while time.monotonic() < deadline:
            # Runtime approval (C2d-5b) owns its Sessions, so it carries no label: any writer row wait.
            if observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database()"
                " AND (application_name='c2d2b_writer' OR :approval) AND wait_event='transactionid')"),
                {"approval": approval}):
                return
            observer.rollback()
            threading.Event().wait(.005)
    raise AssertionError("writer row wait not observed")


@pytest.mark.parametrize("how", ["withdraw", "reject", "approve"])
@pytest.mark.parametrize("winner", ["reader", "writer"])
def test_review_request_item_journal_order_both_winners(reads, monkeypatch, how, winner):
    b = reads
    entered, release = threading.Event(), threading.Event()
    real = boundary.final_caller
    def pause(db, **kw):
        proof = real(db, **kw)
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return proof
    def write():
        with Session(b.w.engine) as db:
            # Approval rolls back the metadata read before copy I/O; preserve
            # only the test observer label across that transaction boundary.
            db.execute(text("SET application_name='c2d2b_writer'"))
            db.commit()
            end_review(b, how, db)
    with ThreadPoolExecutor(2) as pool:
        if winner == "reader":
            monkeypatch.setattr(boundary, "final_caller", pause)
            reader = pool.submit(b.client.get, b.path("admin_get"))
            assert entered.wait(15)
            writer = pool.submit(write)
            try:
                writer_wait(b.w, approval=how == "approve")
                assert not writer.done()
            finally:
                release.set()
            r = reader.result(20)
            writer.result(20)
            assert r.status_code == 200 and r.json()["request"]["review_available"] is True
        elif how == "approve":
            # Runtime approval proves and copies without locks. Pause only AFTER
            # its real request UPDATE/private item SHARE and DML, before journal.
            from app.api import review_approval, review_writes
            gate = review_writes._gate
            def hold_close(db, settings, pending=None):
                if isinstance(pending, review_approval.Approval):
                    entered.set()
                    assert release.wait(15)
                return gate(db, settings, pending)
            monkeypatch.setattr(review_writes, "_gate", hold_close)
            writer = pool.submit(write)
            assert entered.wait(15)
            reader = pool.submit(b.client.get, b.path("admin_get"))
            try:
                wait_for(b.w, "transactionid")
                assert not reader.done()
            finally:
                release.set()
            writer.result(20)
            r = reader.result(20)
            assert r.status_code == 200 and r.json()["request"]["review_available"] is False
            assert not any(marker in r.text for marker in PRIVATE)
        else:
            with Session(b.w.engine) as blocker:
                blocker.execute(select(ReviewRequest.id).where(ReviewRequest.id == b.ids.request).with_for_update())
                b.after_dependency = lambda db, ctx: entered.set()
                reader = pool.submit(b.client.get, b.path("admin_get"))
                try:
                    assert entered.wait(15)
                    wait_for(b.w, "transactionid")
                    end_review(b, how, blocker)
                finally:
                    blocker.rollback()
                r = reader.result(20)
            assert r.status_code == 200 and r.json()["request"]["review_available"] is False
            assert not any(marker in r.text for marker in PRIVATE)
    assert b.w.runtime.pool.checkedout() == 0


@pytest.mark.parametrize("winner", ["reader", "writer"])
def test_common_revoke_item_before_journal_race(reads, monkeypatch, winner):
    b = reads
    entered, release = threading.Event(), threading.Event()
    real = boundary.final_caller
    def pause(db, **kw):
        proof = real(db, **kw)
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return proof
    with ThreadPoolExecutor(2) as pool:
        if winner == "reader":
            monkeypatch.setattr(boundary, "final_caller", pause)
            reader = pool.submit(b.client.get, b.path("pending_get"))
            assert entered.wait(15)
            writer = pool.submit(revoke_copy, b)
            try:
                writer_wait(b.w)
                assert not writer.done()
            finally:
                release.set()
            assert reader.result(20).status_code == 200
            writer.result(20)
        else:
            with Session(b.w.engine) as blocker:
                blocker.execute(select(LibraryItem.id).where(LibraryItem.id == b.copy.item).with_for_update())
                b.after_dependency = lambda db, ctx: entered.set()
                reader = pool.submit(b.client.get, b.path("pending_get"))
                try:
                    assert entered.wait(15)
                    wait_for(b.w, "transactionid")
                    publication.revoke(blocker, b.copy.admin, b.copy.item, RevocationReason.RIGHTS_WITHDRAWN,
                                       NOW, timedelta(hours=24))
                finally:
                    blocker.rollback()
                assert reader.result(20).status_code == 404
    assert b.w.runtime.pool.checkedout() == 0


def test_scope_churn_bounded_no_locks_after_journal(reads, monkeypatch):
    b = reads
    real = reviews._read_list_own
    counter = []
    def churn(*args, **kw):
        data = real(*args, **kw)
        counter.append(1)
        data["total"] += len(counter)
        return data
    monkeypatch.setattr(reviews, "_read_list_own", churn)
    r = b.client.get(b.path("own_list"))
    assert r.status_code == 409 and r.json() == {"error": "resource_scope_changed"}
    assert len(counter) == 6 and b.w.runtime.pool.checkedout() == 0


def test_dirty_session_rejected_before_autoflush(reads):
    b = reads
    with Session(b.w.runtime) as db:
        item = db.get(LibraryItem, b.ids.item)
        item.title = "DIRTY"
        with pytest.raises(RuntimeError, match="clean session"):
            boundary.review_publication_json(db, b.copy.admin, clock=b.clock,
                idle=b.w.settings.auth.session_idle, read=lambda p: {})
        db.rollback()


@pytest.mark.parametrize("key", ["own_get", "admin_get", "pending_get", "copy_get"])
def test_uuid_and_missing_contract_auth_precedence(reads, monkeypatch, key):
    b = reads
    path = b.path(key)
    prefix = path.rsplit("/", 1)[0]
    assert b.client.get(prefix + "/invalid").status_code == 404
    assert b.client.get(prefix + "/" + str(uuid.uuid4())).status_code == 404
    module, name, _ = ROUTES[key]
    real = getattr(module, "_read_" + name)
    def missing(*args, **kw):
        b.at = NOW + timedelta(minutes=30)
        raise ApiError(404, "not_found")
    monkeypatch.setattr(module, "_read_" + name, missing)
    r = b.client.get(path)
    assert r.status_code == 401 and r.json() == {"error": "session_expired"}


@pytest.mark.parametrize("key", ["own_list", "admin_list", "pending_list", "copy_list"])
def test_empty_pages_still_final_caller(reads, monkeypatch, key):
    b = reads
    module, name, _ = ROUTES[key]
    real = getattr(module, "_read_" + name)
    calls = []
    def empty(*args, **kw):
        data = real(*args, **kw)
        calls.append(1)
        assert not data.get("items", data.get("requests", []))
        if len(calls) == 3:
            b.at = NOW + timedelta(minutes=30)
        return data
    monkeypatch.setattr(module, "_read_" + name, empty)
    r = b.client.get(b.path(key) + "?offset=1000")
    assert r.status_code == 401 and r.json() == {"error": "session_expired"}


@pytest.mark.parametrize("key", ["admin_get", "pending_get", "copy_get"])
@pytest.mark.parametrize("seconds,status", [(1799, 200), (1800, 401)])
def test_fresh_clock_after_observed_resource_row_wait(reads, key, seconds, status):
    b = reads
    entered = threading.Event()
    with Session(b.w.engine) as blocker, ThreadPoolExecutor(1) as pool:
        if key == "admin_get":
            blocker.execute(select(ReviewRequest.id).where(ReviewRequest.id == b.ids.request).with_for_update())
        else:
            blocker.execute(select(LibraryItem.id).where(LibraryItem.id == b.copy.item).with_for_update())
        b.after_dependency = lambda db, ctx: entered.set()
        reader = pool.submit(b.client.get, b.path(key))
        try:
            assert entered.wait(15)
            wait_for(b.w, "transactionid")
            b.at = NOW + timedelta(seconds=seconds)
        finally:
            blocker.rollback()
        r = reader.result(20)
        assert r.status_code == status, r.text
    assert b.w.runtime.pool.checkedout() == 0


@pytest.mark.parametrize("key", ["admin_get", "copy_get"])
def test_auth_writer_waits_final_shared_without_user_row_lock(reads, monkeypatch, key):
    b = reads
    entered, release = threading.Event(), threading.Event()
    real = boundary.final_caller
    def pause(db, **kw):
        proof = real(db, **kw)
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return proof
    monkeypatch.setattr(boundary, "final_caller", pause)
    with ThreadPoolExecutor(2) as pool:
        reader = pool.submit(b.client.get, b.path(key))
        assert entered.wait(15)
        writer = pool.submit(close, b.w)  # runtime User -> session -> journal
        try:
            wait_for(b.w, "advisory")
            assert not writer.done()
        finally:
            release.set()
        assert reader.result(20).status_code == 200
        writer.result(20)
    assert b.client.get(b.path(key)).status_code == 401
    assert b.w.runtime.pool.checkedout() == 0


@pytest.mark.parametrize("corruption", ["epoch", "checksum", "version", "file", "publication", "tombstone"])
def test_common_invalid_tuple_closes_flags_history_remains(reads, corruption):
    b = reads
    c = common_copy(b.w, download=True, provider=True)
    if corruption == "tombstone":
        revoke_copy(b, c)
    else:
        sql = {
            "epoch": "UPDATE access_rights SET rights_epoch_at_grant=99 WHERE item_id=:i",
            "checksum": "UPDATE common_publications SET file_checksum_sha256=repeat('f',64) WHERE item_id=:i",
            "version": "UPDATE index_generations SET version_id=gen_random_uuid() WHERE item_id=:i",
            "file": "UPDATE ingestion_jobs SET source_file_id=gen_random_uuid() WHERE item_id=:i",
            "publication": "UPDATE common_publications SET revoked_at=:at WHERE item_id=:i",
        }[corruption]
        with b.w.engine.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            conn.execute(text(sql), {"i": c.item, "at": NOW})
    r = b.client.get(f"/admin/common-copies/{c.item}")
    assert r.status_code == 200, r.text
    row = r.json()["item"]
    assert not row["fulltext_available"] and not row["index"]["search_open"] and not row["index"]["rag_open"]
    # Index version/job pinning is stronger than the independent download gate.
    assert (row["original_download"] == "open") == (corruption in ("version", "file"))


@pytest.mark.parametrize("retain", [False, True])
def test_revoked_cleanup_policy_no_private_source_rehydration(reads, retain):
    b = reads
    bases = (publication.RetentionBasis(RevokedMetadataField.TITLE, "synthetic policy", "history", "synthetic basis"),) if retain else ()
    with Session(b.w.engine) as db:
        publication.revoke(db, b.copy.admin, b.copy.item, RevocationReason.RIGHTS_WITHDRAWN, NOW,
                           timedelta(hours=24), bases)
    with Session(b.w.engine) as db:
        tid = db.scalar(select(DeletionTombstone.id).where(DeletionTombstone.item_id == b.copy.item))
        publication._purge_one(db, MemoryOriginals(b.copy), tid, NOW)
    r = b.client.get(b.path("copy_get"))
    assert r.status_code == 200, r.text
    row = r.json()["item"]
    assert row["title"] == ("Common readable" if retain else None)
    assert row["version"]["source_url"] is None and row["file"]["filename"] is None
    assert all(row["rights"][field] is None for field in
               ("verified_source", "verified_source_url", "license_url", "evidence_note"))
    assert row["tombstone"]["retained_fields"] == (["title"] if retain else [])
    assert b.client.get(f"/library/items/{b.copy.item}").status_code == 404
    assert b.client.get(b.path("pending_get")).status_code == 404
    assert not any(marker in r.text for marker in PRIVATE)
    data = b.client.get("/admin/common-copies?state=revoked&limit=1").json()
    assert data["total"] == 1 and data["items"][0]["id"] == str(b.copy.item)


@pytest.mark.parametrize("winner", ["reader", "cleanup"])
def test_cleanup_tombstone_then_item_no_reader_tombstone_lock(reads, monkeypatch, winner):
    b = reads
    revoke_copy(b)
    with Session(b.w.engine) as db:
        tid = db.scalar(select(DeletionTombstone.id).where(DeletionTombstone.item_id == b.copy.item))
    entered, release = threading.Event(), threading.Event()
    real = boundary.final_caller
    def pause(db, **kw):
        proof = real(db, **kw)
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return proof
    def purge():
        with Session(b.w.engine) as db:
            db.execute(text("SET LOCAL application_name='c2d2b_writer'"))
            return publication._purge_one(db, MemoryOriginals(b.copy), tid, NOW)
    with ThreadPoolExecutor(2) as pool:
        if winner == "reader":
            monkeypatch.setattr(boundary, "final_caller", pause)
            reader = pool.submit(b.client.get, b.path("copy_get"))
            assert entered.wait(15)
            writer = pool.submit(purge)
            try:
                writer_wait(b.w)
                assert not writer.done()
            finally:
                release.set()
            r = reader.result(20)
            writer.result(20)
            assert r.status_code == 200 and r.json()["item"]["title"] == "Common readable"
        else:
            with Session(b.w.engine) as blocker:
                blocker.execute(select(DeletionTombstone.id).where(DeletionTombstone.id == tid).with_for_update())
                blocker.execute(select(LibraryItem.id).where(LibraryItem.id == b.copy.item).with_for_update())
                b.after_dependency = lambda db, ctx: entered.set()
                reader = pool.submit(b.client.get, b.path("copy_get"))
                try:
                    assert entered.wait(15)
                    wait_for(b.w, "transactionid")
                    publication._purge_one(blocker, MemoryOriginals(b.copy), tid, NOW)
                finally:
                    blocker.rollback()
                r = reader.result(20)
            assert r.status_code == 200 and r.json()["item"]["title"] is None
    assert b.w.runtime.pool.checkedout() == 0


def test_only_page_requests_and_granted_items_locked(reads, monkeypatch):
    b = reads
    other = accept(b.w, issue(b.w))
    foreign = request(b.w, other.id, "FOREIGN")
    entered, release = threading.Event(), threading.Event()
    real = boundary.final_caller
    def pause(db, **kw):
        proof = real(db, **kw)
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return proof
    monkeypatch.setattr(boundary, "final_caller", pause)
    with ThreadPoolExecutor(1) as pool:
        reader = pool.submit(b.client.get, b.path("own_list") + "?limit=1")
        try:
            assert entered.wait(15)
            with b.w.engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout='1000ms'"))
                conn.execute(select(ReviewRequest.id).where(ReviewRequest.id == foreign.request).with_for_update())
                conn.execute(select(LibraryItem.id).where(LibraryItem.id == foreign.item).with_for_update())
        finally:
            release.set()
        r = reader.result(20)
    assert r.status_code == 200 and r.json()["total"] == 2
    # Own source plus the approved request used to create the common fixture.
    assert len(r.json()["requests"]) == 1 and "FOREIGN" not in r.text


@pytest.mark.parametrize("key", ADMIN)
def test_current_admin_after_last_resource_sql(reads, monkeypatch, key):
    b = reads
    module, name, _ = ROUTES[key]
    real = getattr(module, "_read_" + name)
    calls = []
    def read(*args, **kw):
        value = real(*args, **kw)
        calls.append(1)
        if len(calls) == 3:
            mutate(b, "role")
        return value
    monkeypatch.setattr(module, "_read_" + name, read)
    r = b.client.get(b.path(key))
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}


@pytest.mark.parametrize("how", ["withdraw", "approve"])
def test_thin_closed_history_still_final_clock(reads, monkeypatch, how):
    b = reads
    end_review(b, how)
    real = reviews._read_admin_get
    calls = []
    def read(*args, **kw):
        value = real(*args, **kw)
        assert not value["request"]["review_available"]
        calls.append(1)
        if len(calls) == 3:
            b.at = NOW + timedelta(minutes=30)
        return value
    monkeypatch.setattr(reviews, "_read_admin_get", read)
    r = b.client.get(b.path("admin_get"))
    assert r.status_code == 401 and not any(marker in r.text for marker in PRIVATE)


def test_review_lock_order_observed_before_journal_and_no_auth_rows(reads):
    from sqlalchemy import Engine, event
    b = reads
    statements = []
    def observed(conn, cursor, statement, parameters, context, executemany):
        if (conn.engine.url.username == "c1_runtime" and conn.engine.url.database == b.w.runtime.url.database
                and ("FOR SHARE" in statement or "pg_advisory_xact_lock_shared" in statement)):
            statements.append(statement)
    # The app has its own pooled Engine for this same runtime LOGIN/database.
    event.listen(Engine, "before_cursor_execute", observed)
    try:
        # Skip entry dependency's own journal proof; require request -> item ->
        # journal specifically in the resource transaction.
        b.after_dependency = lambda db, ctx: statements.clear()
        r = b.client.get(b.path("admin_get"))
    finally:
        event.remove(Engine, "before_cursor_execute", observed)
    assert r.status_code == 200, r.text
    rows = [statement for statement in statements if "FOR SHARE" in statement]
    assert len(rows) == 2
    assert "FOR SHARE OF review_requests" in rows[0]
    assert "FOR SHARE OF library_items" in rows[1]
    assert statements.index(rows[1]) < next(i for i, statement in enumerate(statements)
        if "pg_advisory_xact_lock_shared" in statement)
    assert not any("FOR SHARE OF users" in statement or "FOR SHARE OF sessions" in statement for statement in rows)
